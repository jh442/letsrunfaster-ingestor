"""Authentication manager for Garmin Connect."""

from dataclasses import dataclass
from enum import Enum
import logging
import os
from pathlib import Path
import platform
import sys
from typing import Any, Optional
import garminconnect
from garminconnect import Garmin, GarminConnectAuthenticationError

from . import paths

_LOGGER = logging.getLogger(__name__)

# Login strategies that go through curl_cffi, which bundles its own libcurl
# and OpenSSL. garminconnect tries them first because the TLS fingerprint
# gets past Garmin's WAF, but they are native code: a failure inside them
# kills the process without a Python traceback.
CFFI_STRATEGIES = frozenset({"mobile+cffi", "widget+cffi", "portal+cffi"})


def _on_windows_arm() -> bool:
    return sys.platform == "win32" and platform.machine().lower() in {"arm64", "aarch64"}


def cffi_disabled() -> bool:
    """Whether to skip the curl_cffi login strategies.

    v0.2.0 on Windows ARM64 died with exit code 1 immediately after login
    began, with nothing in the log: the classic shape of a native crash in a
    C extension. The plain ``requests`` strategies cover the same flows, so
    prefer them there. ``LRF_ENABLE_CFFI=1`` opts back in, ``LRF_ENABLE_CFFI=0``
    forces them off everywhere, which is what to try first if another
    platform starts dying the same way.
    """
    override = os.environ.get("LRF_ENABLE_CFFI", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return False
    if override in {"0", "false", "no", "off"}:
        return True
    return _on_windows_arm()


class AuthStatus(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    MFA_REQUIRED = "MFA_REQUIRED"
    FAILED = "FAILED"


@dataclass
class AuthResult:
    status: AuthStatus
    message: str
    display_name: Optional[str] = None
    client_state: Optional[dict[str, Any]] = None


class GarminAuthManager:
    """Manages Garmin Connect login, token caching, and MFA flows."""

    def __init__(self, token_dir: Optional[Path] = None):
        if token_dir is None:
            self.token_dir = paths.token_dir()
        else:
            self.token_dir = Path(token_dir)
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self.token_store = str(self.token_dir)

    def get_client(self, email: Optional[str] = None, password: Optional[str] = None) -> Garmin:
        """Instantiate a Garmin client configured with the session token directory."""
        # No prompt_mfa on purpose: garminconnect then raises
        # GarminConnectAuthenticationError("MFA Required ...") instead of
        # blocking on stdin inside a server thread, and login_with_credentials
        # turns that into the MFA_REQUIRED result the UI's code form handles.
        client = Garmin(email=email, password=password, is_cn=False)
        inner = getattr(client, "client", None)
        if cffi_disabled() and inner is not None and hasattr(inner, "skip_strategies"):
            # The strategy set lives on the inner Client; setting it on the
            # Garmin wrapper would be silently ignored.
            inner.skip_strategies.update(CFFI_STRATEGIES)
            _LOGGER.info(
                "login: using requests strategies only, skipping %s",
                ", ".join(sorted(CFFI_STRATEGIES)),
            )
        return client

    def login_with_tokens(self) -> AuthResult:
        """Try authenticating using existing saved tokens."""
        client = self.get_client()
        try:
            _LOGGER.info("login: restoring cached tokens from %s", self.token_store)
            client.login(tokenstore=self.token_store)
            display_name = client.get_full_name() or "Garmin User"
            _LOGGER.info("login: cached tokens are valid (%s)", display_name)
            return AuthResult(
                status=AuthStatus.AUTHENTICATED,
                message="Successfully authenticated using saved session tokens.",
                display_name=display_name,
            )
        except Exception as e:
            _LOGGER.info("login: cached tokens unusable (%s: %s)", type(e).__name__, e)
            return AuthResult(
                status=AuthStatus.FAILED,
                message=f"Session token restore failed: {str(e)}",
            )

    def login_with_credentials(self, email: str, password: str) -> tuple[AuthResult, Garmin]:
        """Authenticate using email and password, handling MFA if triggered."""
        client = self.get_client(email=email, password=password)
        _LOGGER.info("login: starting password login for %s", email)
        try:
            res = client.login(tokenstore=self.token_store)
            display_name = client.get_full_name() or email
            _LOGGER.info("login: authenticated as %s", display_name)
            return (
                AuthResult(
                    status=AuthStatus.AUTHENTICATED,
                    message="Login successful.",
                    display_name=display_name,
                ),
                client,
            )
        except GarminConnectAuthenticationError as e:
            # Check if this error is an MFA requirement
            # garminconnect raises an auth error with client_state when MFA is required
            client_state = getattr(e, "client_state", None) or getattr(client, "client_state", None)
            _LOGGER.info("login: auth error (%s), client_state=%s", e, bool(client_state))
            if client_state or "mfa" in str(e).lower() or "two-step" in str(e).lower():
                _LOGGER.info("login: MFA code required")
                return (
                    AuthResult(
                        status=AuthStatus.MFA_REQUIRED,
                        message="MFA code required. Please enter the verification code sent to your device/email.",
                        client_state=client_state,
                    ),
                    client,
                )
            return (
                AuthResult(
                    status=AuthStatus.FAILED,
                    message=f"Authentication failed: {str(e)}",
                ),
                client,
            )
        except Exception as e:
            _LOGGER.info("login: failed (%s: %s)", type(e).__name__, e)
            return (
                AuthResult(
                    status=AuthStatus.FAILED,
                    message=f"Login error: {str(e)}",
                ),
                client,
            )

    def submit_mfa(self, client: Garmin, client_state: Optional[dict[str, Any]], mfa_code: str) -> AuthResult:
        """Complete the authentication process by submitting the MFA verification code."""
        try:
            # resume_login takes client_state and mfa_code
            state = client_state or getattr(client, "client_state", {})
            _LOGGER.info("login: submitting MFA code")
            client.resume_login(state, mfa_code)
            # Persist the newly acquired tokens
            if hasattr(client, "garth") and hasattr(client.garth, "dump"):
                client.garth.dump(self.token_store)
            display_name = client.get_full_name() or "Garmin User"
            return AuthResult(
                status=AuthStatus.AUTHENTICATED,
                message="MFA verification successful and session tokens cached.",
                display_name=display_name,
            )
        except Exception as e:
            return AuthResult(
                status=AuthStatus.FAILED,
                message=f"MFA verification failed: {str(e)}",
            )

    def clear_tokens(self) -> bool:
        """Clear cached tokens to log out."""
        try:
            for item in self.token_dir.glob("*"):
                if item.is_file():
                    item.unlink()
            return True
        except Exception:
            return False

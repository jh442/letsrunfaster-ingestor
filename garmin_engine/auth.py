"""Authentication manager for Garmin Connect."""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from typing import Any, Optional
import garminconnect
from garminconnect import Garmin, GarminConnectAuthenticationError


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
            home = Path.home()
            self.token_dir = home / ".garmin-ingestor" / "tokens"
        else:
            self.token_dir = Path(token_dir)
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self.token_store = str(self.token_dir)

    def get_client(self, email: Optional[str] = None, password: Optional[str] = None) -> Garmin:
        """Instantiate a Garmin client configured with the session token directory."""
        return Garmin(email=email, password=password, is_cn=False)

    def login_with_tokens(self) -> AuthResult:
        """Try authenticating using existing saved tokens."""
        client = self.get_client()
        try:
            client.login(tokenstore=self.token_store)
            display_name = client.get_full_name() or "Garmin User"
            return AuthResult(
                status=AuthStatus.AUTHENTICATED,
                message="Successfully authenticated using saved session tokens.",
                display_name=display_name,
            )
        except Exception as e:
            return AuthResult(
                status=AuthStatus.FAILED,
                message=f"Session token restore failed: {str(e)}",
            )

    def login_with_credentials(self, email: str, password: str) -> tuple[AuthResult, Garmin]:
        """Authenticate using email and password, handling MFA if triggered."""
        client = self.get_client(email=email, password=password)
        try:
            res = client.login(tokenstore=self.token_store)
            display_name = client.get_full_name() or email
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
            if client_state or "mfa" in str(e).lower() or "two-step" in str(e).lower():
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

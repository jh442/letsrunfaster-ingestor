/**
 * Desktop bridge.
 *
 * In the browser build there is no bridge: every capability below degrades to
 * a plain web behaviour, so the same bundle runs in both places. Inside
 * Electron the preload script exposes `window.lrfDesktop`, and that is the
 * only place native capabilities may live. Nothing here touches Node.
 */

const bridge = typeof window !== 'undefined' ? window.lrfDesktop : null;

/** True when the page is running inside the Electron shell. */
export const isDesktop = Boolean(bridge);

/**
 * Open the OS folder picker.
 *
 * Resolves to an absolute path, or `null` when the user cancelled, the
 * platform refused, or we are in a browser (where a folder's absolute path
 * is deliberately unreadable).
 *
 * @param {{ title?: string, defaultPath?: string }} options
 * @returns {Promise<string | null>}
 */
export async function pickDirectory(options = {}) {
  if (!bridge || typeof bridge.pickDirectory !== 'function') return null;
  try {
    const picked = await bridge.pickDirectory(options);
    return typeof picked === 'string' && picked.trim() ? picked : null;
  } catch (err) {
    console.error('Folder picker failed', err);
    return null;
  }
}

/** Version reported by the shell, or null in the browser. */
export function appVersion() {
  return (bridge && bridge.appVersion) || null;
}

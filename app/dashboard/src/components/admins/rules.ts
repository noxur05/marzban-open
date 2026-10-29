// The backend's rules for admins (app/models/admin.py), so a bad value fails
// on its field instead of as a 422.

export const USERNAME_MAX = 32;
export const PASSWORD_MIN = 6;
// No '?', '#' or 'user@': the token path is appended after it.
const SUBSCRIPTION_URL_RE = /^https?:\/\/[^\s/?#@]+(\/[^\s?#]*)?$/;

/** Code points, as Python's len() counts them. */
export const codePoints = (v: string) => [...v].length;

export const isSubscriptionUrl = (v: string) => SUBSCRIPTION_URL_RE.test(v) && v.length <= 512;

/** Why a new admin's name is refused (a locale key), or null. Names go into
 * URL paths, so ASCII letters, digits and _ . @ - only. */
export const usernameProblem = (v: string): string | null => {
  if (v.length < 3 || v.length > USERNAME_MAX) return "admins.errors.usernameLength";
  if (!/^[A-Za-z0-9_.@-]+$/.test(v)) return "admins.errors.usernameChars";
  if (!/^[A-Za-z0-9]/.test(v)) return "admins.errors.usernameStart";
  return null;
};

// No 0/O, 1/l/I: the password is read off a phone and typed on another.
const ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

export const generatePassword = (length = 12) => {
  const out: string[] = [];
  const buf = new Uint8Array(1);
  while (out.length < length) {
    crypto.getRandomValues(buf);
    // Rejection sampling: 256 is not a multiple of the alphabet's size.
    if (buf[0] < 256 - (256 % ALPHABET.length)) out.push(ALPHABET[buf[0] % ALPHABET.length]);
  }
  return out.join("");
};

# Changes summary

The user authentication module is implemented in pure Python using a dictionary to store users and their passwords. The module handles email validation, password strength checks, PBKDF2 hashing, HMAC-SHA256 signing, and rate-limiting. The module includes functions for registering new users with strong passwords and logging in with the correct credentials, as well as rate-limiting to prevent brute-force attacks.

## Files

- `src/user_authentication.py` — Implementation of the user authentication module in pure Python using a dictionary to store users and their passwords. The module handles email validation, password strength checks, PBKDF2 hashing, HMAC-SHA256 signing, and rate-limiting.

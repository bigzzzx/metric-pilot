# Local account authentication design

## Goal

Replace the hard-coded local user with local registration and login while
keeping the project runnable without an external identity provider.

## Scope

Users register with a unique username and password, log in, log out, and retain
an authenticated session through an HttpOnly cookie. This first version has no
roles or email verification. Platform model providers, data sources, and Agent
definitions remain shared administrator-managed configuration; conversations
and user-created knowledge bases are private to their owner.

## Backend

SQLite gains `users` and `auth_sessions` tables. Passwords use PBKDF2-HMAC-SHA256
with per-password random salt and a constant-time comparison. Sessions use a
random opaque token stored server-side, expire after seven days, and are sent in
a `HttpOnly`, `SameSite=Lax` cookie. Auth helpers resolve the current user from
the cookie and all protected API routes fail with structured 401 errors when no
valid session exists.

The existing `sessions` chat records gain a `user_id` column. Knowledge bases
gain an `owner_id` column; the default business knowledge base remains shared.
Legacy local chat data is retained but is not exposed as a signed-in user’s
conversation.

## Frontend

The app starts on a compact login/register screen when unauthenticated. On
success it loads the workspace, renders the signed-in username in the sidebar,
and provides a logout action. Fetch calls include cookies automatically. The
existing local browser conversation state remains per page for this iteration;
server-side chat history is scoped by user ID.

## Verification

Tests cover password verification, registration/login/logout, rejection of
protected requests without a cookie, and per-user history isolation. The login
screen and production build are verified after implementation.

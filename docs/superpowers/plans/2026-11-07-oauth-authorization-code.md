# OAuth Authorization Code Login Implementation Plan

**Goal:** Let a ServiceNow instance be configured with an OAuth `client_id` and `client_secret`, let `snow login` sign the user in through the browser, and use the resulting bearer token for all REST-based commands and MCP tools.

**Architecture:** Each instance gets an auth type (`AuthType.BASIC`, the default, or `AuthType.OAUTH`). A new `snow_cli/token_store.py` keeps tokens in the OS keyring. A new `snow_cli/oauth.py` runs the browser login, exchanges and refreshes tokens, and exposes them through a `requests` auth object. `ServiceNowClient` remains the only facade over HTTP: it selects the auth for its REST session and exposes `authenticate()` and `supports_ui_session`, so commands never touch tokens. All existing `rest_get` call sites stay untouched. `snow run` and `snow elevate` fail with a clear error under OAuth.

**Tech Stack:** Python (keep `requires-python >=3.8` syntax), requests, click, keyring, standard library `http.server` and `webbrowser`, pytest. No new dependencies.

**Why this grant:** The first design used `client_credentials`. The test instance refused it with `access_denied`, and it has no `glide.oauth.inbound.client.credential.grant_type.enabled` property. That property applies to the whole instance, so setting it is an administrator's decision. The authorization code grant needs only a redirect URL on the OAuth client, runs as the signed-in user, and works with the login method the instance already uses.

## Global Constraints

* Existing instances without an `auth` key keep working as basic auth, with no migration.
* The client secret and tokens never appear on the command line, in output, in logs, or in `repr(Config)`.
* The token request is a URL-encoded POST body, not JSON.
* Token requests never follow redirects. `requests` keeps the method and body on 307 and 308, which would send the secret to the redirect target.
* Nothing on the user's ServiceNow instance may be changed by tests or checks. No scripts, record writes or configuration changes. See Task 2 for what a sign-in leaves behind.
* All network behaviour is unit-tested with mocked HTTP responses, in the style of `tests/test_client.py`.
* New tests use a real `Config` or `Mock(spec=Config)`. A bare `Mock()` makes `config.is_oauth` truthy and hides wrong branches.

## Design Decisions

### What works under OAuth

| Feature | Transport | Under OAuth |
|---|---|---|
| `snow login` | Browser, then token endpoint | Signs in and stores tokens |
| `snow record *`, `snow r *`, `snow table *` | REST | Works with a bearer token |
| MCP search, count, aggregate, fields tools | REST | Works after `snow login` |
| `snow_login` MCP tool | None | Clear error: run `snow login` in a terminal |
| `snow run`, `snow run --auto-login`, `snow_run_script` | Scraping `/sys.scripts.do` | Clear error |
| `snow elevate`, `snow_elevate` | Scraping `/navpage.do` | Clear error |

The scraping commands depend on a cookie session created by posting a password to `/login.do`. An OAuth instance has no stored password. Proving that a bearer token could run a script would mean running one on the instance, so that stays out of scope unless the user approves it.

### Client type

Use a private client with its existing secret, plus the `state` parameter. ServiceNow's documentation gives private clients the secret-based code exchange and refresh tokens. PKCE and public clients are left out: sources disagree on public clients, and the documentation covers refresh tokens for private clients only.

### Sign-in flow

1. Generate `state` with `secrets.token_urlsafe(32)`.
2. Bind `127.0.0.1` on the redirect port before opening the browser, so a busy port fails early with a clear message.
3. Print the URL `https://<instance>/oauth_auth.do?response_type=code&client_id=...&redirect_uri=...&state=...` and open it with `webbrowser.open`. Printing it covers SSH sessions with a forwarded port.
4. Accept `GET /callback`. Compare `state` with `hmac.compare_digest`. Requests with a wrong `state` get a 400 and do not end the wait. An `error` parameter raises `OAuthError`. Give up after 180 seconds, which leaves time for SSO and MFA.
5. Answer with a fixed plain-text page. Never echo query values into the response.
6. POST `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `client_secret` to `/oauth_token.do`, then store the tokens.

The redirect URI is the constant `http://localhost:8765/callback`, which must be registered on the client. It becomes configurable only if Task 2 shows the need.

### Token lifecycle

1. Use the stored access token if it has more than 60 seconds left.
2. Otherwise POST `grant_type=refresh_token` and store the result. Keep the old refresh token if the response has no new one.
3. If the token endpoint rejects the refresh, or there are no stored tokens, raise `OAuthLoginRequired`, whose message says to run `snow login`. Network failures stay plain `OAuthError`, so an outage does not ask for a new sign-in.
4. On a 401 from a REST call, refresh and retry the request once. A second 401 is returned to the caller unchanged.

### Storage

```json
{
  "instances": {
    "dev1234.service-now.com": {
      "auth": "oauth",
      "client_id": "abc123",
      "keyring": true
    }
  }
}
```

* Client secret in keyring: service `snow-cli:<instance>`, account `oauth:<client_id>`. Keyring unavailable: `"client_secret"` in `config.json` (mode 600) plus the existing warning.
* Tokens in keyring: same service, account `oauth-token:<client_id>`, as one JSON value holding `access_token`, `refresh_token` and `expires_at`. A refresh token is a long-lived credential, so it gets the same protection as a password.
* Keyring unavailable: the same JSON in `Config.token_cache_file` (`~/.snow-run/tmp/<instance>/oauth_token.json`), mode 600, written atomically (temp file, then `os.replace`), plus a warning.
* Switching client, or switching auth type, removes stored tokens.

### Credential resolution

First match wins:

1. `snow_client_id` and `snow_client_secret` both set: OAuth. Setting only one is an error.
2. `snow_user` and `snow_pwd` both set: basic.
3. The stored instance, using its `auth` value. No `auth` key means basic.

Env vars supply the client, not a session. A prior `snow login` is still needed.

### Deliberately not built

* **Client credentials and JWT bearer grants.** Both suit unattended use and need instance-level setup. `BearerAuth` and `TokenStore` would carry over if one is added later.
* **Unattended first login.** A person must sign in once, so a fresh CI run cannot use OAuth.
* **`BasicCredentials` / `OAuthCredentials` class hierarchy** and abstract provider interfaces. Auth type branches in only a few places, and each interface would have one implementation.

---

### Task 1: Fix MCP credential loading (done)

Handled in its own PR, on `fix/mcp-credential-loading`.

* All MCP tools load config through `_load_config(instance)` in `snow_cli/mcp_server.py`, inside their `try`.
* `_load_config` refuses instances that are not configured and are not `snow_instance`.
* `Config.ensure_instance_set()` requires a bare hostname with an optional port.
* MCP `output_file` is confined to the server's working directory, and `saved_to` is the absolute path.
* `client_secret` and `access_token` are in `_REDACTED_PARAMS`. Task 5 adds `refresh_token`.
* The audit log records real command outcomes and the resolved host.

### Task 2: Spike against a real instance (done)

**Nothing on the instance may be changed.** The user runs the spike in their own terminal, so the secret never reaches the chat.

Prerequisite: `http://localhost:8765/callback` is listed as a Redirect URL on the OAuth client. If the user has to add it, that is their change to their own client.

Requests made:

* The browser opens `/oauth_auth.do`. The user signs in and may see a consent screen.
* `POST /oauth_token.do` exchanges the code. The instance records the issued tokens, and this cannot be avoided when testing OAuth.
* `GET /api/now/table/sys_db_object?sysparm_limit=1` confirms the token works on REST.
* `GET /sys.scripts.do` shows whether a bearer token reaches the UI page. The form is never submitted.
* `POST /oauth_token.do` with the refresh token, once, shows whether refresh tokens rotate.

- [ ] **Step 1: Run the spike script.** It saves `tests/fixtures/oauth_token_response.json` and `tests/fixtures/oauth_refresh_response.json` with token values replaced by placeholders.
- [ ] **Step 2: Record the outcomes under "Spike Results" below.** If `expires_in` can be missing, default to 1800 seconds and rely on the 401 path. Otherwise treat its absence as an `OAuthError`.

Tasks 3 and 6-8 do not depend on the result. Tasks 4 and 5 use the fixtures.

### Task 3: Config model and env resolution (done)

`AuthType`, `client_id`, `client_secret`, `token_cache_file` and `Config.is_oauth` exist, secrets are out of `repr`, env resolution follows the rules above, and the CLI reports a half-set env without a traceback.

### Task 4: Token store, token requests and client wiring (done)

**Files:**
* Create: `snow_cli/timeouts.py`, `snow_cli/token_store.py`, `snow_cli/oauth.py`
* Modify: `snow_cli/client.py`
* Test: `tests/test_token_store.py`, `tests/test_oauth.py` (new), `tests/test_client.py`

**Interfaces:**
* In `snow_cli/token_store.py`:
  * `class Tokens(NamedTuple)` with `access_token: str`, `refresh_token: Optional[str]`, `expires_at: float`
  * `class TokenStore` built from an instance, a client ID and a fallback path, with `load() -> Optional[Tokens]`, `save(tokens)`, `clear()`
* In `snow_cli/oauth.py`:
  * `class OAuthError(RuntimeError)`, with subclasses `OAuthRejected` (the token endpoint refused the request) and `OAuthLoginRequired`. Existing `except RuntimeError` handlers in REST commands already print these cleanly.
  * `exchange_code(config, code, now) -> Tokens`, `refresh_tokens(config, refresh_token, now) -> Tokens`, sharing one private POST helper
  * `class OAuthTokenProvider` with `get_token(force_refresh: bool = False) -> str`, built from a `Config`, a `TokenStore`, and `now=time.time`
  * `class BearerAuth(requests.auth.AuthBase)` wrapping a provider
* In `snow_cli/client.py`:
  * `ServiceNowClient.supports_ui_session -> bool`

- [ ] **Step 1: Write failing tests**

  `TokenStore`:
  * Round-trips tokens through the keyring, leaving no file.
  * Falls back to a mode 600 file with a warning when the keyring fails.
  * A corrupt value loads as `None`.
  * `clear()` removes both locations.
  * A failing keyring read does not raise.

  Token requests:
  * `exchange_code` and `refresh_tokens` POST the right form fields to `/oauth_token.do`, with `timeout=DEFAULT_TIMEOUT` and `allow_redirects=False`.
  * Both parse the fixtures from Task 2, and compute `expires_at` from the injected `now`.
  * Any 3xx raises `OAuthError` and sends nothing further.
  * Error responses raise `OAuthError` using `error_description`, then `error`. A 401 with `error=server_error` and `error_description=access_denied` is the shape seen on a real instance. The message never contains the secret.
  * A non-JSON response (for example a hibernating developer instance) raises `OAuthError` with the HTTP status.

  `OAuthTokenProvider`, with a fake store and a fake clock:
  * A valid access token makes no request.
  * A token within 60 seconds of expiry is refreshed and stored.
  * A refresh response without `refresh_token` keeps the old one.
  * A rejected refresh raises `OAuthLoginRequired`. A network failure raises plain `OAuthError`.
  * No stored tokens raises `OAuthLoginRequired`.

  `BearerAuth`:
  * Sets `Authorization: Bearer <token>`.
  * On 401 it refreshes and resends once, and the final response has the 401 in `history`.
  * On a second 401 it returns the response with no further request.

  `ServiceNowClient`:
  * Uses `BearerAuth` for an OAuth config. Existing `test_rest_session_has_basic_auth_pre_set` passes unchanged.
  * `supports_ui_session` is `True` for basic and `False` for OAuth.
- [ ] **Step 2: Implement.** Move the timeout constants from `client.py` to `timeouts.py`, because `client.py` imports `oauth.py`. `BearerAuth`:
  ```python
  class BearerAuth(requests.auth.AuthBase):
      def __init__(self, provider):
          self._provider = provider

      def __call__(self, request):
          request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
          request.register_hook("response", self._retry_on_401)
          return request

      def _retry_on_401(self, response, **kwargs):
          if response.status_code != 401:
              return response
          retry = response.request.copy()
          retry.headers["Authorization"] = f"Bearer {self._provider.get_token(force_refresh=True)}"
          # copy() shares the hooks with the original request; removing ours limits this to one retry.
          retry.deregister_hook("response", self._retry_on_401)
          response.content  # release the connection
          response.close()
          retried = response.connection.send(retry, **kwargs)
          retried.history.append(response)
          retried.request = retry
          return retried
  ```
  Token requests call `requests.post` directly, never the client's REST session, which would recurse into `BearerAuth`.
- [ ] **Step 3: Wire into the client.** Choose the auth in one place, in `ServiceNowClient.__init__`. Update the class docstring.
- [ ] **Step 4: Run tests, commit** `feat: authenticate REST calls with OAuth bearer tokens`

### Task 5: Browser login and command behaviour (done)

**Files:**
* Modify: `snow_cli/oauth.py`, `snow_cli/client.py`, `snow_cli/commands.py`, `snow_cli/mcp_server.py`, `snow_cli/audit.py`
* Test: `tests/test_oauth.py`, `tests/test_commands.py`, `tests/test_mcp_server.py`

**Interfaces:**
* `oauth.authorization_url(config, state) -> str`
* `oauth.CallbackServer(state, port)` with `wait_for_code(timeout) -> str`
* `oauth.browser_login(config, store) -> None`
* `ServiceNowClient.authenticate() -> None`

- [ ] **Step 1: Write failing tests**

  Login, against a real local server on port 0 with `webbrowser.open` patched:
  * The authorization URL contains the expected parameters, correctly encoded, and no secret.
  * The server binds `127.0.0.1` only.
  * A correct callback returns the code.
  * A wrong `state` gets a 400 and keeps waiting.
  * An `error` callback raises `OAuthError`.
  * The response page does not echo query values.
  * A timeout raises `OAuthError`.
  * A busy port raises `OAuthError` naming the port, before the browser opens.

  Commands:
  * `login()` under OAuth calls `client.authenticate()`, prints success, returns 0, and never touches `/login.do`.
  * `login()` under OAuth returns 1 and prints the reason on `OAuthError`.
  * `elevate()` and `run_script()` under OAuth return 1 with a message that names the limitation, and make no HTTP call.
  * `run_script()` under OAuth fails before reading the script from stdin or a file.
  * `run_script(auto_login=True)` under OAuth does not attempt login.
  * MCP `snow_login` on an OAuth instance raises an error telling the user to run `snow login`, and never opens a browser.
  * `refresh_token` is redacted by `_redact`.
- [ ] **Step 2: Implement**
  * `login()`: when `not client.supports_ui_session`, call `client.authenticate()` and return. Commands stay unaware of tokens.
  * Add `_ensure_ui_session(client)` in `commands.py`, raising `ValueError`. Call it in `elevate()` and `run_script()`. Their outer `except Exception` handlers print it and return 1.
  * In `run_script()`, move `client = ServiceNowClient(config)` above the script reading.
  * Suggested message: `Background scripts and elevation need a UI session, which OAuth sign-in does not create. Configure this instance with a username and password to use this command.`
- [ ] **Step 3: Run tests, commit** `feat: add browser login and guard session commands`
- [ ] **Step 4: Tracer check.** With `snow_instance`, `snow_client_id` and `snow_client_secret` set, run `snow login`, then `snow r search sys_db_object -l 1`. Stop and fix anything found before continuing.

### Task 6: Stored OAuth credentials (done)

**Files:**
* Modify: `snow_cli/instance_repository.py`
* Test: `tests/test_instance_repository.py`

**Interfaces:**
* `InstanceRepository.save_oauth(instance, client_id, client_secret, set_default=False) -> bool`

- [ ] **Step 1: Write failing tests**
  * `save_oauth` stores the secret in keyring under `oauth:<client_id>` and writes no secret to JSON.
  * Keyring failure stores `client_secret` in JSON and returns `False`.
  * `load_config` returns an OAuth `Config` from a stored instance.
  * An entry with no `auth` key loads as basic.
  * Switching an instance between basic and OAuth removes the old keyring entry and stored tokens.
  * `remove()` deletes the OAuth secret and tokens. It currently deletes by `user` only, so both would be orphaned.
- [ ] **Step 2: Implement**
  * Reuse the existing keyring helpers, passing `f"oauth:{client_id}"` as the account.
  * Add a private `_delete_stored_secret(instance, entry)` that handles both entry types, and use it from `save`, `save_oauth` and `remove`.
- [ ] **Step 3: Run tests, commit** `feat: store OAuth client credentials per instance`

### Task 7: `snow add --oauth` and instance display (done)

**Files:**
* Modify: `snow_cli/instance_manager.py`, `snow_cli/cli.py`
* Test: `tests/test_instance_manager.py` (new)

**Interfaces:**
* `add_oauth_instance(instance=None, client_id=None, set_default=False) -> int`. A separate function, not a flag on `add_instance`.

- [ ] **Step 1: Write failing tests**
  * `add_oauth_instance` prompts for the client ID if not supplied, and always reads the secret through `getpass`.
  * An empty client ID or secret returns 1.
  * Output never contains the secret, and it says to run `snow login` next.
  * `list_instances()` handles an OAuth entry. It reads `creds['user']` directly today, which raises `KeyError`.
  * `show_info()` shows the auth type and whether the secret is set, for both types.
- [ ] **Step 2: Implement**
  * Add `--oauth` and `--client-id` to `add`, dispatching to `add_oauth_instance`. There is deliberately no `--client-secret` option, because it would land in shell history.
  * `snow list` prints `Auth: OAuth authorization code`, the client ID, and the secret's storage location.
- [ ] **Step 3: Run tests, commit** `feat: add --oauth option to snow add`

### Task 8: Documentation (done)

**Files:**
* Modify: `README.md`, `SECURITY.md`, `copilot-skills/servicenow-mcp/SKILL.md`

- [ ] **Step 1: README.** Add a setup section with `snow add <instance> --oauth` and `snow login`, the env vars, the resolution order, and the feature table above.
- [ ] **Step 2: Instance checklist in README**
  * Create an OAuth client for external clients: System OAuth > Application Registry, or All > Machine Identity Console on newer releases.
  * Add `http://localhost:8765/callback` as its Redirect URL.
  * No system property is needed.
- [ ] **Step 3: SECURITY.md.** Cover secret and token storage, refresh tokens as long-lived credentials, the file fallback, and the loopback callback.
- [ ] **Step 4: SKILL.md.** Note that OAuth instances need `snow login` in a terminal, and that `snow_login`, `snow_run_script` and `snow_elevate` are unavailable on them.
- [ ] **Step 5: Commit** `docs: describe OAuth sign-in setup`

### Task 9: End-to-end check

On a non-production instance, using read-only commands only. No script is run there:

- [ ] `snow add <instance> --oauth`, then `snow login`
- [ ] `snow r search sys_db_object -l 1`, `snow r count incident`, `snow table fields incident`
- [ ] Change `expires_at` in stored tokens to the past, and confirm a refresh happens
- [ ] Corrupt the access token, leaving `expires_at` alone, and confirm a REST command recovers via the 401 retry
- [ ] Corrupt the refresh token too, and confirm the error says to run `snow login`
- [ ] Set only `snow_client_id` and confirm a clear error
- [ ] `snow run example.js` and `snow elevate` print the guard message
- [ ] An MCP search works against the OAuth instance, and `snow_login` gives its error
- [ ] `snow remove <instance>` clears the secret and tokens
- [ ] An existing basic instance still works: `snow login`, `snow r search`

## Spike Results

Run on 2026-11-07 against the user's dev instance, read-only.

* The code exchange returned `access_token`, `refresh_token`, `scope`, `token_type` and `expires_in`. Its lifespan was 1799 seconds. A missing `expires_in` is therefore an `OAuthError`.
* The refresh returned a `refresh_token` identical to the old one, so tokens did not rotate on this instance. The cross-process reload was dropped from the design.
* The token scope comes from the client. Other clients may grant different access.
* A bearer token works on the REST table API (HTTP 200).
* `GET /sys.scripts.do` returned HTTP 200 with one `sysparm_ck`. That proves nothing: `/login.do` contains `sysparm_ck` too, so this may be the login page. The guard on `snow run` stands.
* A wrong client secret returns HTTP 401 with `error=server_error`, `error_description=access_denied`. So does a `client_credentials` request, whose cause was never established: the missing property and a wrong secret are both possible.

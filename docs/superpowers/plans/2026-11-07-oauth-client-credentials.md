# OAuth Client Credentials Login Implementation Plan

**Goal:** Let a ServiceNow instance be configured with an OAuth `client_id` and `client_secret` instead of a username and password, and use the resulting bearer token for all REST-based commands and MCP tools.

**Architecture:** Each instance gets an auth type (`AuthType.BASIC`, the default, or `AuthType.OAUTH`). A new `snow_cli/oauth.py` module fetches and caches tokens through the `client_credentials` grant and exposes them through a `requests` auth object. `ServiceNowClient` remains the only facade over HTTP: it selects the auth for its REST session and exposes `authenticate()` and `supports_ui_session`, so commands never touch tokens. All existing `rest_get` call sites stay untouched. Commands that need a UI session (`snow run`, `snow elevate`) fail with a clear error under OAuth, unless the spike in Task 2 shows a bearer token can drive them.

**Tech Stack:** Python (keep `requires-python >=3.8` syntax), requests, click, keyring, pytest. No new dependencies.

**Order of work:** Fix the MCP bug first, because it stands alone. Then test the real service, then build a thin end-to-end slice driven by env vars (Tasks 3-5). Stored credentials and `snow add` come last, once the slice works against a real instance.

## Global Constraints

* Existing instances without an `auth` key keep working as basic auth, with no migration.
* The client secret never appears on the command line, in output, in logs, or in `repr(Config)`.
* The secret goes to the OS keyring, with the same config-file fallback and warning as passwords.
* The token request is a URL-encoded POST body, not JSON.
* The token request never follows redirects. `requests` keeps the method and body on 307 and 308, which would send the secret to the redirect target.
* All network behaviour is unit-tested with mocked HTTP responses, in the style of `tests/test_client.py`.
* New tests use a real `Config` or `Mock(spec=Config)`. A bare `Mock()` makes `config.is_oauth` truthy and hides wrong branches.

## Design Decisions

### Grant type

Use `client_credentials` only. It is the only grant that needs just a client ID and secret. The password grant still needs a username and password, so it adds nothing over the existing basic auth flow. Authorization code and JWT bearer are out of scope.

### What works under OAuth

| Feature | Transport | Under OAuth |
|---|---|---|
| `snow record *`, `snow r *`, `snow table *` | REST | Works with a bearer token |
| MCP search, count, aggregate, fields tools | REST | Works once Task 1 is done |
| `snow login` | Token endpoint | Fetches and caches a token, verifying the credentials |
| `snow run`, `snow run --auto-login`, `snow_run_script` | Scraping `/sys.scripts.do` | Clear error, pending Task 2 |
| `snow elevate`, `snow_elevate` | Scraping `/navpage.do` | Clear error, pending Task 2 |

The scraping commands depend on a cookie session created by posting a password to `/login.do`. An OAuth instance has no password, so the current flow cannot work.

### Credential resolution

First match wins:

1. `snow_client_id` and `snow_client_secret` both set: OAuth.
2. `snow_user` and `snow_pwd` both set: basic.
3. The stored instance, using its `auth` value. No `auth` key means basic.

Rule 1 is strict: setting only one of the two OAuth variables is an error from `load_config`, not a silent fallback to basic. Existing per-field merging of `snow_user` and `snow_pwd` with stored basic credentials stays as it is. On a stored OAuth instance, setting only one of those two has no effect.

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

* Secret in keyring: service `snow-cli:<instance>`, account `oauth:<client_id>`. The prefix keeps it from colliding with a user account of the same name.
* Keyring unavailable: `"client_secret"` in `config.json` (mode 600) plus the existing warning.
* Token cache: `~/.snow-run/tmp/<instance>/oauth_token.json`, mode 600, holding `client_id`, `access_token`, `expires_at`. Written atomically (temp file, then `os.replace`), because MCP and CLI processes can run at once. If two processes refresh together, both tokens are valid and the last write wins.
* The cache path reaches `oauth.py` as a value, `Config.token_cache_file`, computed in `load_config` like `cookie_file`. Neither `client.py` nor `oauth.py` imports `InstanceRepository`.

### Token lifecycle

1. Use the cached token if `client_id` matches and it has more than 60 seconds left.
2. Otherwise POST to `https://<instance>/oauth_token.do` and cache the result. Task 2 settles how a missing `expires_in` is handled.
3. On a 401 from a REST call, fetch a new token, overwrite the cache, and retry the request once. A second 401 is returned to the caller unchanged.

No refresh token handling: the client can always request a new token with its credentials, so a refresh token would add code without adding capability.

### Deliberately not built

* **`BasicCredentials` / `OAuthCredentials` class hierarchy.** Auth type branches in only a few places and no third type is planned. Revisit if one appears.
* **Abstract `TokenProvider` or `TokenStore` interfaces.** There is one implementation of each.
* **REST request timeouts.** These are handled separately on `claude/upbeat-torvalds-90e7ad`, which changes `snow_cli/client.py`. Merge it before Task 4. The new token request sets its own timeout either way.

---

### Task 1: Fix MCP credential loading (done)

Committed on `fix/mcp-credential-loading` for its own PR, in `4618f82` and `b91934a`. It is an existing bug, and OAuth cannot reach MCP without it.

* All MCP tools load config through `_load_config(instance)` in `snow_cli/mcp_server.py`, inside their `try`, so failures reach the audit log.
* `_load_config` refuses instances that are not configured and are not `snow_instance`, because `load_config` applies env credentials to any host. `snow_client_id` and `snow_client_secret` get the same protection.
* `Config.ensure_instance_set()` requires a bare hostname with an optional port.
* MCP `output_file` is confined to the server's working directory, and `saved_to` is the absolute path.
* `client_secret` and `access_token` are in `_REDACTED_PARAMS`.

### Task 2: Spike against a real instance

**Needs a non-production instance with an OAuth client. Only the fixture and the recorded results are committed.**

- [ ] **Step 1: Configure the instance** using the checklist in Task 8, with an application user who has the `admin` role.
- [ ] **Step 2: Get a token and keep the raw response**
  ```bash
  read -s CLIENT_SECRET
  curl -s https://$INSTANCE/oauth_token.do \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode client_id=$CLIENT_ID \
    --data-urlencode client_secret=$CLIENT_SECRET > /tmp/snow-oauth-token.json
  TOKEN=$(python3 -c 'import json; print(json.load(open("/tmp/snow-oauth-token.json"))["access_token"])')
  ```
- [ ] **Step 3: Save a fixture.** Copy the response to `tests/fixtures/oauth_token_response.json` with `access_token` and any other token values replaced by placeholders. Then delete `/tmp/snow-oauth-token.json`.
- [ ] **Step 4: Check the failure shape.** Repeat the request with a wrong secret and record the status and body. Task 4's error handling is written against this.
- [ ] **Step 5: Probe the script page and look for a token in the form**
  ```bash
  curl -s -c /tmp/snow-oauth-spike.txt -H "Authorization: Bearer $TOKEN" https://$INSTANCE/sys.scripts.do | grep -c sysparm_ck
  ```
- [ ] **Step 6: If `sysparm_ck` is present, POST a `gs.print` script** with the token and the saved cookies. Delete `/tmp/snow-oauth-spike.txt` afterwards.
- [ ] **Step 7: Record the outcomes under "Spike Results" below.**
  * If `expires_in` is always present, treat its absence as an `OAuthError`. If it can be missing, default to 1800 seconds and rely on the 401 path.
  * If the script page works, add a follow-up task that sends the bearer header on the scraping session and lifts Task 5's guard for `snow run`. If it does not, Task 5 stands as written.

Tasks 3-8 do not depend on the script page result.

### Task 3: Config model and env resolution (done)

**Files:**
* Modify: `snow_cli/config.py`, `snow_cli/instance_repository.py`, `snow_cli/cli.py`
* Test: `tests/test_config.py`, `tests/test_instance_repository.py`, `tests/test_commands.py`

**Interfaces:**
* Produces:
  * `class AuthType(str, Enum)` with `BASIC` and `OAUTH`, following `OutputFormat`
  * `Config.auth_type: AuthType = AuthType.BASIC`
  * `Config.client_id: Optional[str]`, `Config.client_secret: Optional[str] = field(default=None, repr=False)`
  * `Config.token_cache_file: Optional[Path]`
  * `Config.is_oauth -> bool`

- [ ] **Step 1: Write failing tests**
  * Default `auth_type` is `AuthType.BASIC`.
  * `ensure_credentials_set()` under OAuth passes with `client_id` and `client_secret`, with no `user`.
  * It raises under OAuth when either is missing, and the message names `snow add <instance> --oauth`.
  * Basic behaviour and message are unchanged.
  * `repr(Config(client_secret="s3cret", password="p4ss"))` contains neither value.
  * `load_config` follows each rule under "Credential resolution".
  * `load_config` with only one of `snow_client_id`, `snow_client_secret` raises `ValueError`.
  * `load_config` sets `token_cache_file` to `tmp/<instance>/oauth_token.json`.
- [ ] **Step 2: Implement**
  * Add a separate env branch to `load_config`. Leave the existing basic path alone.
  * Add `InstanceRepository.token_cache_file_for(instance)`, next to `cookie_file_for`.
  * In `cli.main`, turn the `ValueError` from `load_config` into `click.ClickException`, so a half-set env gives an error rather than a traceback.
- [ ] **Step 3: Run tests, commit** `feat: add OAuth fields to Config`

### Task 4: Token fetch, cache and client wiring

**Files:**
* Create: `snow_cli/oauth.py`
* Modify: `snow_cli/client.py`
* Test: `tests/test_oauth.py` (new), `tests/test_client.py`

**Interfaces:**
* Produces in `snow_cli/oauth.py`:
  * `class OAuthError(RuntimeError)`. Existing `except RuntimeError` handlers in REST commands already print it cleanly.
  * `class Token(NamedTuple)` with `access_token: str`, `expires_at: float`
  * `fetch_token(base_url: str, client_id: str, client_secret: str, now: Callable[[], float]) -> Token`
  * `class TokenCache` with `load() -> Optional[Token]` and `save(token)`, built from a path and a client ID
  * `class OAuthTokenProvider` with `get_token(force_refresh: bool = False) -> str`, built from a zero-argument fetch callable, a `TokenCache`, and `now=time.time`
  * `class BearerAuth(requests.auth.AuthBase)` wrapping a provider
* Produces in `snow_cli/client.py`:
  * `ServiceNowClient.supports_ui_session -> bool`
  * `ServiceNowClient.authenticate() -> None`, which forces a new token and raises `OAuthError` on failure

- [ ] **Step 1: Write failing tests**

  `fetch_token`:
  * POSTs form data with `grant_type`, `client_id`, `client_secret` to `/oauth_token.do`, with `timeout=30` and `allow_redirects=False`.
  * Parses `tests/fixtures/oauth_token_response.json`, and computes `expires_at` from the injected `now`.
  * Any 3xx raises `OAuthError` and sends nothing further.
  * Error responses raise `OAuthError` with the server's `error` and `error_description`, matching the shape recorded in Task 2. The message never contains the secret.
  * A non-JSON response (for example a hibernating developer instance) raises `OAuthError` with the HTTP status.

  `TokenCache`:
  * Round-trips a token. The file has mode 600.
  * A different `client_id`, or a corrupt file, loads as `None`.

  `OAuthTokenProvider`, with a fake fetch and a fake clock:
  * A valid cached token makes no fetch.
  * A token within 60 seconds of expiry is refetched.
  * `force_refresh=True` always fetches and overwrites the cache.

  `BearerAuth`:
  * Sets `Authorization: Bearer <token>`.
  * On 401 it fetches a new token and resends once, and the final response has the 401 in `history`.
  * On a second 401 it returns the response with no further request.

  `ServiceNowClient`:
  * Uses `BearerAuth` for an OAuth config. Existing `test_rest_session_has_basic_auth_pre_set` passes unchanged.
  * `supports_ui_session` is `True` for basic and `False` for OAuth.
- [ ] **Step 2: Implement `snow_cli/oauth.py`**
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
  `fetch_token` calls `requests.post` directly, never the client's REST session, which would recurse into `BearerAuth`.
- [ ] **Step 3: Wire into the client.** Choose the auth in one place, in `ServiceNowClient.__init__`: build the provider and `BearerAuth` when `config.is_oauth`, otherwise keep the existing tuple. Update the class docstring.
- [ ] **Step 4: Run tests, commit** `feat: authenticate REST calls with OAuth bearer tokens`
- [ ] **Step 5: Tracer check.** With `snow_instance`, `snow_client_id` and `snow_client_secret` set, run `snow r search sys_user -l 1` against the spike instance. Stop and fix anything found before continuing.

### Task 5: Command behaviour under OAuth

**Files:**
* Modify: `snow_cli/commands.py`
* Test: `tests/test_commands.py`

- [ ] **Step 1: Write failing tests**
  * `login()` under OAuth calls `client.authenticate()`, prints success, returns 0, and never touches `/login.do`.
  * `login()` under OAuth returns 1 and prints the reason on `OAuthError`.
  * `elevate()` and `run_script()` under OAuth return 1 with a message that names the limitation, and make no HTTP call.
  * `run_script()` under OAuth fails before reading the script from stdin or a file.
  * `run_script(auto_login=True)` under OAuth does not attempt login.
- [ ] **Step 2: Implement**
  * `login()`: when `not client.supports_ui_session`, call `client.authenticate()` and return. Commands stay unaware of tokens.
  * Add `_ensure_ui_session(client)` in `commands.py`, raising `ValueError`. Call it in `elevate()` and `run_script()`. Their outer `except Exception` handlers print it and return 1.
  * In `run_script()`, move `client = ServiceNowClient(config)` above the script reading.
  * Suggested message: `Background scripts and elevation need a UI session, which OAuth client credentials cannot create. Configure this instance with a username and password to use this command.`
- [ ] **Step 3: Run tests, commit** `feat: support OAuth in login and guard session commands`

### Task 6: Stored OAuth credentials

**Files:**
* Modify: `snow_cli/instance_repository.py`
* Test: `tests/test_instance_repository.py`

**Interfaces:**
* Produces: `InstanceRepository.save_oauth(instance, client_id, client_secret, set_default=False) -> bool`

- [ ] **Step 1: Write failing tests**
  * `save_oauth` stores the secret in keyring under `oauth:<client_id>` and writes no secret to JSON.
  * Keyring failure stores `client_secret` in JSON and returns `False`.
  * `load_config` returns an OAuth `Config` from a stored instance.
  * An entry with no `auth` key loads as basic.
  * Switching an instance between basic and OAuth removes the old keyring entry and the token cache.
  * `remove()` deletes the OAuth keyring entry and the token cache. It currently deletes by `user` only, so an OAuth secret would be orphaned.
- [ ] **Step 2: Implement**
  * Reuse the existing keyring helpers, passing `f"oauth:{client_id}"` as the account.
  * Add a private `_delete_stored_secret(instance, entry)` that handles both entry types, and use it from `save`, `save_oauth` and `remove`.
- [ ] **Step 3: Run tests, commit** `feat: store OAuth client credentials per instance`

### Task 7: `snow add --oauth` and instance display

**Files:**
* Modify: `snow_cli/instance_manager.py`, `snow_cli/cli.py`
* Test: `tests/test_instance_manager.py` (new)

**Interfaces:**
* Produces: `add_oauth_instance(instance=None, client_id=None, set_default=False) -> int`. A separate function, not a flag on `add_instance`, which stays as it is.

- [ ] **Step 1: Write failing tests**
  * `add_oauth_instance` prompts for the client ID if not supplied, and always reads the secret through `getpass`.
  * An empty client ID or secret returns 1.
  * Output never contains the secret.
  * `list_instances()` handles an OAuth entry. It reads `creds['user']` directly today, which raises `KeyError`.
  * `show_info()` shows the auth type and whether the secret is set, for both types.
- [ ] **Step 2: Implement**
  * Add `--oauth` and `--client-id` to `add`, dispatching to `add_oauth_instance`. There is deliberately no `--client-secret` option, because it would land in shell history.
  * `snow list` prints `Auth: OAuth client credentials`, the client ID, and the secret's storage location.
- [ ] **Step 3: Run tests, commit** `feat: add --oauth option to snow add`

### Task 8: Documentation

**Files:**
* Modify: `README.md`, `SECURITY.md`, `copilot-skills/servicenow-mcp/SKILL.md`

- [ ] **Step 1: README.** Add a setup section with `snow add <instance> --oauth`, the env vars, the resolution order, and the feature table above.
- [ ] **Step 2: Instance checklist in README**
  * Inbound client credentials need the Washington DC release or later.
  * Set the system property `glide.oauth.inbound.client.credential.grant_type.enabled` to `true`, in Global scope. It may already be set on Zurich or later.
  * Create the OAuth client: System OAuth > Application Registry, or All > Machine Identity Console > Inbound integrations on newer releases.
  * Set the OAuth Application User. All calls run as this user, with its roles and ACLs.
  * `access_denied` usually means the property is off or the application user is missing or inactive.
- [ ] **Step 3: SECURITY.md.** Cover secret storage, the token cache file, and the point that the application user sets the blast radius, so it should have the fewest roles that will do the job. Note that the token cache is a credential while it is valid, unlike the claim there that cookies are not sensitive.
- [ ] **Step 4: SKILL.md.** Note that `snow_run_script` and `snow_elevate` are unavailable on OAuth instances.
- [ ] **Step 5: Commit** `docs: describe OAuth client credentials setup`

### Task 9: End-to-end check

On a non-production instance:

- [ ] `snow add <instance> --oauth`, then `snow login`
- [ ] `snow r search sys_user -l 1`, `snow r count incident`, `snow table fields incident`
- [ ] Change `access_token` in the cache to an invalid value, leaving `expires_at` alone, and confirm a REST command recovers via the 401 retry
- [ ] Set only `snow_client_id` and confirm a clear error
- [ ] `snow run example.js` and `snow elevate` print the guard message
- [ ] An MCP search works against the OAuth instance
- [ ] `snow remove <instance>` clears the keyring entry and `oauth_token.json`
- [ ] An existing basic instance still works: `snow login`, `snow run`, `snow r search`

## Spike Results

To be filled in by Task 2:

* Do background scripts work with a bearer token?
* Is `expires_in` always returned, and what is the token lifespan?
* What do a failed token request's status and body look like?

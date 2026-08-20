# Mail runtime and installation contract

Ars Operandi contains one provider-aware runtime package. The installed skill
is documentation/discovery only; it is not a copied runner. Codex starts the
runtime from the canonical Ars checkout through the managed `ars-mail` STDIO
MCP alias.

## Session lifecycle

- One MCP process is one credential session.
- Proton Bridge credential resolution happens once per dedicated Keychain item and is cached only in process memory.
- Process exit drops the cache; no secret is written to a file, environment variable, marker, MCP configuration, result, error, or log.
- Restarting Codex or the MCP may require one new Keychain authorization.
- GWS keeps encrypted OAuth state in the isolated profile config. Only its encryption key uses the native Keychain through gws's shared OS-user keyring entry. Interactive OAuth remains CLI-only.
- Every GWS identity or mailbox read first parses `gws auth status` and fails closed unless the selected profile reports OAuth2, encrypted storage, native `keyring`, an available client config with a non-empty project id, decryptable encrypted credentials, a refresh token, a valid token, and exactly `gmail.readonly` plus the `openid`, `userinfo.email`, and `userinfo.profile` scopes that gws adds for identity. The GWS process `HOME` must equal the selected absolute profile config, making the OS user's global well-known ADC path unreachable. Before every GWS command, the runtime rejects profile-local `credentials.json` and `.config/gcloud/application_default_credentials.json` without reading or identifying them in its error. Plaintext, ambient credential sources, ADC fallback, and every other service or Gmail scope are forbidden.
- One `GwsMailClient` caches only a successful semantic status check, avoiding repeated status probes while one operation reads multiple messages. Each normal runtime operation creates a fresh client and rechecks; interactive authentication clears the cache before login and again after token-cache invalidation.
- GWS readiness failures expose only one fixed code/message pair: `gws_auth_status_invalid`, `gws_auth_backend_invalid`, `gws_auth_source_invalid`, `gws_auth_decryption_failed`, `gws_auth_invalid_client`, `gws_auth_token_invalid`, `gws_auth_scopes_invalid`, or `gws_identity_mismatch`. Raw provider status and errors are never returned.
- Every operation reloads and validates the explicit manifest and rechecks binding, provider, exact identity, and provider safety policy.
- Searches remain metadata-only and bounded. Selected message content requires one opaque message id and a finite byte limit.
- Attachment retrieval requires both opaque ids, a finite byte limit, and a new absolute output path; the runtime refuses overwrite and symlink targets, writes mode `0600`, and never auto-opens or executes the file.

The deployed Keychain service namespace is deliberately preserved during the
ownership cutover. Its name is a credential compatibility reference, not a
runtime owner. Changing it would be a separate explicit credential migration.

## Dry-run and activation

Run from the canonical Ars checkout:

```bash
python scripts/install_mail_runtime.py install --runtime-repo "<ars-operandi-repo>" --project-index "<project-index>" --config-root "<config-root>"
```

If an earlier unmarked copy of `mailctl-email-access` is installed, inspect it
first and add `--replace-existing-skill` explicitly. The installer preserves a
rollback copy. Apply only after reviewing the dry-run:

```bash
python scripts/install_mail_runtime.py install --runtime-repo "<ars-operandi-repo>" --project-index "<project-index>" --config-root "<config-root>" --replace-existing-skill --apply
python scripts/install_mail_runtime.py smoke
```

Fresh live proof after activation must cover both configured providers with
sanitized status, exact-identity onboarding, a maximum-one bounded search,
one explicitly selected message envelope, and one explicitly selected
attachment written to a new local file. Do not retain message content,
attachment bytes, provider transcripts, or secrets as verification logs.

After a human-approved GWS reauthorization, durability proof requires the exact
profile to pass sanitized status and identity checks in two fresh processes
without a second OAuth prompt. A successful browser callback in one process is
not persistence proof, and the proof does not require reading email.

The installer fails closed on an unmanaged `ars-mail` MCP, managed skill drift,
environment-bearing MCP configuration, a non-Ars runtime root, or an unavailable
manifest. It never installs a LaunchAgent or stores credentials.

## Rollback

Preview and then apply rollback:

```bash
python scripts/install_mail_runtime.py rollback
python scripts/install_mail_runtime.py rollback --apply
```

Rollback removes only installer-managed MCP state and the managed skill. If the
activation replaced an earlier skill copy, rollback restores its preserved
backup. It does not touch provider OAuth, Keychain items, Proton Bridge, the
Project Index, or the transitional source repository.

Consumer invocation and Project Index ownership changes are a separate atomic
consumer lane after the Ars change lands and passes fresh-session live proof.

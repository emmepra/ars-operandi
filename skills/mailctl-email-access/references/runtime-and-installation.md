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
- GWS keeps OAuth state in its isolated provider keyring. Interactive OAuth remains CLI-only.
- Every operation reloads and validates the explicit manifest and rechecks binding, provider, exact identity, and provider safety policy.

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

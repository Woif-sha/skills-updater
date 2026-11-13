# Maintain REA And Its Bundled Skill

Use this workflow for `reverse-engineer-anything`, including an unmanaged entry
reported as `unknown_version` by the Git-only Python CLI. That status describes
missing Git provenance; the agent must check the owning npm installation before
reporting REA's update status. This workflow supplements the CLI; it does not add
npm support to its JSON schema.

The maintained source is [morluto/rea](https://github.com/morluto/rea), published as
`rea-agents`. Read its current [installation and maintenance guide](https://github.com/morluto/rea/blob/main/docs/getting-started/installation.md)
when choosing commands. Preserve `local-only`: if set on the installed Skill,
report it and stop this workflow before any remote lookup or mutation.

## Check The Owning Installation

1. Read the current client's REA MCP registration. For Codex this is
   `mcp_servers.rea` in `~/.codex/config.toml`. Resolve its actual Node command,
   REA entry script and owning `package.json`; verify package name `rea-agents`
   and repository `morluto/rea`. For a CLI-only installation, resolve `rea` and
   the package that owns it. A registration into npm's `_npx` cache belongs to
   the package runner, not the global npm prefix. Missing or ambiguous ownership
   is an error; do not substitute whichever `rea` happens to be on PATH.
2. Read that package's version, run its selected Node executable with
   `--version`, and query `npm view rea-agents dist-tags.latest --json` through
   its npm registry. Query the exact release's `engines.node` before choosing an
   upgrade runtime. Check the installed Skill against the same package's
   `skills/reverse-engineer-anything` directory, including references. Report
   local/latest npm versions, runtime compatibility, and whether the Skill
   matches the installed package. Do not stop at the Git CLI's `unknown_version`.
3. A check-only request ends here. Keep the npm semver separate from
   `installedBaseVersion`; do not invent `.openskills.json` Git provenance or
   edit the generated registry to make this package appear Git-managed.
4. For default or all-Skill updates, update REA only when the npm release is
   newer, and refresh the Skill only when it differs from the owning package
   or the package was upgraded. If both already match, report no action. A
   Skill-refresh-only request uses the current package's `--skill-client` setup
   scope below; it does not upgrade REA or Node. Honor explicit runtime migration
   requests separately.

## Update And Refresh

1. Use an existing compatible system Node runtime. On this user's Windows host,
   prefer system nvm over WorkBuddy's bundled Node installation. When a runtime
   upgrade is authorized and needed, inspect `nvm list` and official Node release
   metadata, then install a compatible stable release with `nvm install VERSION 64`
   and select it with `nvm use VERSION`. Verify the actual Node version and npm
   prefix. Do not upgrade Node for a read-only check.
2. For an installation already owned by the selected npm global prefix, invoke
   that installation's `rea update --json`. For an explicitly authorized move
   from a bundled runtime to system nvm, install the exact resolved release with
   `npm install --global rea-agents@VERSION`, then verify its owning package and
   CLI version. If setup classifies the old launcher as foreign, verify the old
   package's identity, back up the client configuration, and replace only that
   REA registration's command and entry script with the verified nvm paths under
   the authorized migration. Retain its arguments, environment and other fields,
   then preview setup again. An existing package-runner installation uses a
   version-pinned `npx rea-agents@VERSION` invocation of setup. Keep failures
   visible; do not silently switch sources or installation mechanisms.
3. From that same verified installation, preview
   `rea setup --client codex --dry-run --json` (substitute the affected client).
   Inspect exact configuration paths, Skill destinations, backups, and retained
   provider settings. Use the reviewed scope and existing user authorization to
   apply `rea setup --client codex --yes --json`. Select only affected existing
   clients. When only the bundled Skill needs refreshing, use
   `rea setup --skill-client codex --dry-run --json`, then apply that scope with
   `--yes --json`. Use the installed release's help if its options differ.
4. Read back the MCP command/entrypoint, owning npm version, and installed Skill
   tree. Verify they bind to the new installation and its matching bundled Skill.
   Preserve existing provider settings. Run a scoped
   `rea doctor --client codex --skill --json` and report failed checks precisely.
   File readiness does not prove the active MCP connection has reloaded: restart
   or reconnect the affected client before claiming its live server is upgraded.

Refresh the Skill through the owning REA package's setup, rather than installing
repository-main instructions independently. Keep this workflow outside the
REA-installed Skill directory so setup cannot overwrite the maintenance route.

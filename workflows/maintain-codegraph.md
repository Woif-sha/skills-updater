# Maintain CodeGraph And Agent Integration

Use this workflow for the installed CodeGraph from
[colbymchenry/codegraph](https://github.com/colbymchenry/codegraph), npm package
`@colbymchenry/codegraph`. Read its current
[installation and upgrade instructions](https://github.com/colbymchenry/codegraph#get-started)
and the installed CLI's help before maintenance. Do not select a similarly named
Python, Go or Rust project from search results.

CodeGraph need not appear under `~/.agents/skills`. Its CLI and MCP registration
are maintained here by the agent, outside the Python CLI's Git registry schema.
Use the official installer to refresh managed instructions; do not invent a
`codegraph skill` command or Git provenance for this installation.

## Check

1. Read the current client's MCP registration; for Codex, inspect
   `mcp_servers.codegraph` in `~/.codex/config.toml` (honor an explicit
   `CODEX_HOME`). Resolve its launcher and owning installation. Confirm the npm
   package name and repository, or the standalone bundle's release identity.
   Record `<installed launcher> --version`. Preserve existing registration
   arguments and environment, including any tool filters or project selection.
2. If a registered bare `codegraph` command is missing from PATH, report the
   broken registration separately from version status. On nvm hosts, inspect
   the installed versions' global launchers and package metadata: switching
   Node versions does not migrate global npm tools. Do not report missing from
   the current prefix as never installed, or acquire an unverified substitute.
3. Run the verified launcher with `upgrade --check`. It reports current/latest
   versions in human-readable output; it has no JSON option. Classify the
   actual result, retaining lookup errors. Check a reported release against
   this repository's releases or `npm view @colbymchenry/codegraph dist-tags.latest`.
   npm and standalone channels must retain their own release identity.
4. Report installed/latest versions, installation path and whether the MCP
   launcher resolves. A check-only request ends here without installing,
   refreshing agent configuration, starting servers or indexing projects.

## Update Or Restore The Launcher

1. For an authorized update, use `<installed launcher> upgrade` (or
   `upgrade VERSION` for an explicit pin). If the version is already current
   and integration is healthy, report no action. For npm installations, first
   verify that the active `npm config get prefix` owns this launcher: the
   upgrader invokes npm from PATH, so a different nvm version updates a
   different prefix. Use the owning prefix, or treat an authorized move to the
   current nvm version as a migration with an exact verified package version:
   `npm install --global @colbymchenry/codegraph@VERSION`.
2. When restoring a command after an authorized Node switch, reinstalling that
   exact CodeGraph version in the current prefix restores availability without
   implying a new release. Verify its package and launcher version afterward.
   The npm launcher uses a matching platform package with a bundled Node
   runtime; do not upgrade system Node solely for CodeGraph.
3. Review `install --help` and `install --print-config codex` from the verified
   installation. The latter previews the MCP snippet only, not the instruction
   block. Back up the affected client's config and managed instruction file.
   For Codex-only maintenance use
   `codegraph install --target codex --location global --yes` from that
   installation. In 1.6.2, `install --refresh` ignores `--target` and refreshes
   all configured agents; `upgrade` invokes that broad refresh automatically.
   Account for this scope before upgrading or selecting a refresh command.
   The installer rewrites Codex's CodeGraph main table, so preserve and restore
   custom arguments, environment and timeout settings; verify unrelated tables
   and text outside the fenced instruction block remain unchanged. Do not
   repeat an already completed refresh unnecessarily.
4. Verify the configured launcher, CLI version, and CodeGraph's fenced agent
   instruction block. Test the MCP initialize response from a new process to
   confirm its version. A workspace without `.codegraph/` can legitimately
   expose no tools; do not run `init`, pass `--init`, rebuild or delete indexes
   merely to validate an upgrade. Restart or reconnect the affected client
   before claiming its existing MCP connection runs the new installation.

Keep this maintenance workflow in skills-updater so CodeGraph's installer cannot
overwrite it when refreshing its own agent instructions.

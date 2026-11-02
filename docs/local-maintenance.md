# Custom builds and Magpie integration

The `custom` branch extends the upstream OpenFic source. Build instructions and integration contracts belong in this repository; machine-specific deployment records, data inventories, credentials and recovery locations belong outside Git.

## Magpie integration

Use the [OpenFic Agent plugin](https://github.com/kombier/magpie-plugins/tree/main/packages/openfic) with an OpenFic build containing `app.models.managed`. Upstream OpenFic releases do not read this integration configuration.

Magpie supplies the local gateway connection and the models made available to OpenFic. Choose default, light, summary, compaction, role and session models in OpenFic itself. Model names, notes and generation parameters also remain application settings.

The plugin writes the `magpie` object in `~/.config/openfic/magpie.json`, using configuration version 2. It does not write a default model. The backend accepts version 1 catalogs for compatibility, but ignores their former top-level default-model selection. An isolated instance can use `OPENFIC_MAGPIE_CONFIG` to select a separate configuration file.

The current plugin accepts explicit model IDs under `zhipu/`, `minimax-cn/` and `chatgpt-api/`. These are Magpie provider identifiers; configure matching API-backed providers before connecting. An identifier alone does not prove the provider's upstream service or billing method. Check provider endpoints and fallback settings against the intended channel policy. Subscription providers and route groups are not accepted by this plugin.

OpenFic reads one catalog snapshot per database session and creates durable model identities through its own ORM. Refreshing the catalog updates connection and catalog metadata without overwriting local model choices or generation parameters. Repeated imports are idempotent.

Hiding a referenced model makes it unavailable during management; it does not delete its record, reset preferences or silently select another model. Reopening it preserves its identity and parameters. Empty catalogs are valid. Invalid configuration reports an error rather than switching to another channel.

Disconnecting restores Magpie's managed configuration keys and preserves OpenFic's local settings and imported manual Magpie models. Continuing to use those models still requires the Magpie gateway. Refreshing affects subsequent model resolution; clients already running do not switch connections. Restart the whole desktop application when migrating from an older integration.

The integration does not manage Skill, MCP, prompts, creative permissions or local embedding/rerank. Model visibility describes normal client behavior, not an access-control boundary against a malicious client. Never commit the generated configuration or expose its gateway key in logs.

## Source-build differences

The branch includes a bundled backend wheel and build-identity checks, chapter reordering, targeted fake-IP handling, provider-name editing support, and desktop layout/window adjustments. The desktop backend uses a fixed port and listens on all interfaces; review network access and application authentication before deployment.

Source changes are maintained in Git. Do not patch compressed frontend bundles, `app.asar`, installed application files or a separate replay script.

## Build

Use a clean, committed checkout with the repository's locked dependencies. The custom packaging path currently targets macOS arm64 and requires Node, the declared pnpm version, uv and macOS signing tools.

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir desktop install --frozen-lockfile
pnpm --dir desktop run package:custom
```

`package:custom` builds the wheel and its frontend, desktop UI and main process from the same commit. It creates `desktop/dist-electron/mac-arm64/OpenFic.app`, signs it locally and verifies the signature. It does not install or publish the application and does not provide Apple notarization.

`desktop/resources/custom-backend/manifest.json` records the source commit, upstream tag, wheel SHA256 and build identity. Startup validates the bundled wheel and the backend's matching identity; a missing or invalid bundle is an error rather than permission to install an unrelated package. Python and third-party runtime dependencies still use the application's native installation flow.

## Validate and deploy

Run the relevant backend tests and frontend type, lint and format checks. Validate the packaged application separately from development mode.

For an isolated desktop instance, set `OPENFIC_CUSTOM_USER_DATA_DIR` to a private absolute directory and ensure its desktop configuration points every test instance at separate data. Merely copying the App does not isolate its data. Start the complete application executable so it inherits the isolation environment.

Check application health, bundled-backend identity, representative tool calls, and connect/refresh/hide/disconnect/reconnect behavior. Verify that local model choices and parameters survive catalog changes and that only the intended API channels are used. Use test projects and data rather than production work.

Before replacing an installed build, stop the whole application and protect its application/runtime/configuration and a consistent data backup with the matching encryption key. Installation is a separate operation from packaging. Never overwrite live databases or change unrelated clients as part of plugin validation.

## Update and recover

Track upstream releases separately from deployment. Integrate a selected stable tag into `custom`, review which customizations are still necessary, then build and validate the matching App/wheel before installation.

Keep application, backend and data versions compatible during recovery. Check database migration compatibility before reverting a build. Store local recovery records and secrets outside this repository. Markdown exports are not a replacement for a complete data backup and its encryption key.

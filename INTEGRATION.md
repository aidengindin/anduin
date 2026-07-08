# NixOS integration guide for `anduin`

Instructions for a future agent (or future-me) to wire this repo into
`/home/agindin/code/nixos-config` and deploy to `osgiliath`. This doc assumes
the repo at `github:aidengindin/anduin` exposes the same `packages.<system>.anduin`
output the local flake here does.

The conventions here are not invented; they mirror what `headache-sync` already
does in nixos-config. When in doubt, **read `services/headache-sync.nix` and
`hosts/osgiliath/services.nix` and copy the shape.**

## High-level shape

Two NixOS modules and one host wire-up:

1. **`services/anduin-postgres.nix`** — declarative `nixos-container` running
   `postgresql_16` + `timescaledb` extension. Host-only bridge network, port
   forward to `127.0.0.1:5433`. Backup job runs on the host and dumps over the
   bridge. Container state lives at `/var/lib/nixos-containers/anduin-postgres/`
   and must be persisted through impermanence.
2. **`services/anduin.nix`** — one module exposing
   `agindin.services.anduin.enable` plus per-source `<source>.enable`
   sub-options. Creates a system user `anduin`, declares one oneshot+timer per
   enabled source (google-health, withings, intervals, liftosaur), declares one
   one-shot `anduin-db-migrate` service, all consuming the agenix-decrypted env
   file path and a JSON config file via `ANDUIN_CONFIG`.
3. **Host wiring in `hosts/osgiliath/services.nix`** — enable both modules,
   declare the agenix secret(s), pass non-secret config through.

Why a container for postgres? `services.postgresql` is a singleton on NixOS;
adding the TimescaleDB extension to the shared cluster (immich, linkwarden,
grafana, miniflux, arr) would couple Timescale's planner hooks and extension
upgrade cadence to every tenant. A declarative container gives clean isolation
without leaving NixOS idioms.

## Step 0: add the flake input

In `nixos-config/flake.nix`, after `auto-headache-tracker`:

```nix
anduin = {
  url = "git+ssh://git@github.com/aidengindin/anduin.git";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Add `anduin` to the outputs argument list and to both `customPkgs` invocations
(`standardSpecialArgs` and the bottom `packages.x86_64-linux` block).

In `nixos-config/packages/default.nix`:

```nix
{
  pkgs,
  unstablePkgs,
  auto-headache-tracker,
  anduin,
  ...
}:
{
  # ...existing entries...
  anduin = anduin.packages.${pkgs.system}.anduin;
}
```

## Step 1: write `services/anduin-postgres.nix`

The scaffolding is in this repo's git history (a draft was authored alongside
this doc but not committed to nixos-config). Reproduce it with these
constraints, **and verify each option against current NixOS module docs at the
target nixpkgs channel before deploying**:

- `agindin.services.anduin-postgres.enable` (mkEnableOption).
- `hostPort` option, default `5433`. Forward `containerPort = 5432` → that host
  port.
- Container `containers.anduin-postgres`:
  - `autoStart = true`, `privateNetwork = true`
  - `hostAddress = "192.168.100.1"`, `localAddress = "192.168.100.2"` (pick a
    free /30; verify nothing else on osgiliath uses 192.168.100.0/24).
  - `forwardPorts = [ { containerPort = 5432; hostPort = cfg.hostPort; protocol = "tcp"; } ]`.
  - Container `config = { pkgs, ... }: { ... }`:
    - `system.stateVersion` = matches host channel.
    - `services.postgresql.enable = true`, `package = pkgs.postgresql_16`,
      `extensions = ps: [ ps.timescaledb ]`,
      `settings = { port = 5432; listen_addresses = lib.mkForce "192.168.100.2"; shared_preload_libraries = "timescaledb"; }`.
    - `ensureDatabases = [ "anduin" ]`, `ensureUsers` with `anduin`
      (ensureDBOwnership) and `anduin_ro` (read-only).
    - `authentication = lib.mkForce` with `local all all trust`,
      `host all anduin 192.168.100.1/32 trust`, and same for `anduin_ro`. The
      container has no other network exit, so trust on a single host IP is
      acceptable (mirrors the security level of a Unix socket).
    - `networking.firewall.enable = false;` inside the container.
- Host-side:
  - `agindin.impermanence.systemDirectories` += `/var/lib/nixos-containers/anduin-postgres` and `/var/backup/postgres-anduin` (when impermanence enabled).
  - `agindin.services.restic.paths` += `/var/backup/postgres-anduin` (when restic enabled).
  - `systemd.tmpfiles.rules`: create `/var/backup/postgres-anduin` 0750 root root.
  - `systemd.services.anduin-postgres-backup` oneshot that runs
    `pg_dump -h 192.168.100.2 -p 5432 -U anduin -Fc anduin > /var/backup/postgres-anduin/anduin.dump.tmp && mv ... anduin.dump`.
    `after`/`requires` = `container@anduin-postgres.service`. Reuse hardening
    block from `services/postgres.nix`'s `postgres-backup` service.
  - `systemd.timers.anduin-postgres-backup` with `OnCalendar = cfg.backupTimerOnCalendar` (default `daily`) and `Persistent = true`.

Register the module in `services/default.nix` alongside the existing entries.

## Step 2: write `services/anduin.nix`

Use `services/headache-sync.nix` as the template — copy its hardening block
verbatim, copy the JSON-config-via-env-var pattern, copy the user/group
creation. Per-source structure:

```nix
{
  config,
  lib,
  pkgs,
  customPkgs,
  ...
}:
let
  cfg = config.agindin.services.anduin;
  inherit (lib) mkIf mkEnableOption mkOption types;

  configJson = pkgs.writeText "anduin-config.json" (builtins.toJSON {
    state_dir = "/var/lib/anduin/state";
    google_health = {
      enabled = cfg.google-health.enable;
      backfill_window_days = cfg.google-health.backfillWindowDays;
    };
    withings = {
      enabled = cfg.withings.enable;
      window_days = cfg.withings.windowDays;
    };
    intervals = {
      enabled = cfg.intervals.enable;
      window_days = cfg.intervals.windowDays;
      pull_streams = cfg.intervals.pullStreams;
    };
    liftosaur = {
      enabled = cfg.liftosaur.enable;
      window_days = cfg.liftosaur.windowDays;
    };
  });

  hardening = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectClock = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectHostname = true;
    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    RestrictNamespaces = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    CapabilityBoundingSet = "";
    SystemCallFilter = [ "@system-service" "~@resources" "~@privileged" ];
    SystemCallArchitectures = "native";
  };

  mkExtractor = name: schedule: {
    description = "anduin ${name} extractor";
    after = [ "network-online.target" "anduin-db-migrate.service" ];
    wants = [ "network-online.target" ];
    requires = [ "anduin-db-migrate.service" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      User = cfg.user;
      Group = cfg.group;
      EnvironmentFile = cfg.environmentFile;
      Environment = [ "ANDUIN_CONFIG=${configJson}" ];
      StateDirectory = "anduin/state/${name}";
      StateDirectoryMode = "0700";
      ExecStart = "${cfg.package}/bin/anduin extract ${name}";
    };
  };
in {
  options.agindin.services.anduin = {
    enable = mkEnableOption "anduin health data pipeline";
    package = mkOption {
      type = types.package;
      default = customPkgs.anduin;
    };
    user = mkOption { type = types.str; default = "anduin"; };
    group = mkOption { type = types.str; default = "anduin"; };

    environmentFile = mkOption {
      type = types.path;
      description = ''
        Agenix-decrypted env file. Must define DATABASE_URL and the secrets for
        each enabled source. See INTEGRATION.md for the full variable list.
      '';
    };

    google-health = {
      enable = mkEnableOption "Google Health (Fitbit Air) extractor";
      schedule = mkOption { type = types.listOf types.str; default = [
        "*-*-* *:17:00"             # hourly today-window pull
        "*-*-* 02:30:00"            # nightly 7-day backfill
      ]; };
      backfillWindowDays = mkOption { type = types.ints.positive; default = 7; };
    };
    withings = {
      enable = mkEnableOption "Withings (body weight) extractor";
      schedule = mkOption { type = types.listOf types.str; default = [
        "*-*-* 03,09,15,21:00:00"   # every 6h
      ]; };
      windowDays = mkOption { type = types.ints.positive; default = 14; };
    };
    intervals = {
      enable = mkEnableOption "intervals.icu extractor";
      schedule = mkOption { type = types.listOf types.str; default = [
        "*-*-* *:23:00"             # hourly
      ]; };
      windowDays = mkOption { type = types.ints.positive; default = 3; };
      pullStreams = mkOption { type = types.bool; default = true; };
    };
    liftosaur = {
      enable = mkEnableOption "Liftosaur extractor";
      schedule = mkOption { type = types.listOf types.str; default = [
        "*-*-* *:43:00"             # hourly
      ]; };
      windowDays = mkOption { type = types.ints.positive; default = 7; };
    };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      description = "anduin service user";
    };
    users.groups.${cfg.group} = { };

    # Persist OAuth tokens through impermanence rebuilds.
    agindin.impermanence.systemDirectories =
      mkIf config.agindin.impermanence.enable [ "/var/lib/anduin" ];

    systemd.services.anduin-db-migrate = {
      description = "anduin DB migrations (idempotent)";
      after = [ "container@anduin-postgres.service" ];
      requires = [ "container@anduin-postgres.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = hardening // {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        Environment = [ "ANDUIN_CONFIG=${configJson}" ];
        ExecStart = "${cfg.package}/bin/anduin db migrate";
      };
    };

    systemd.services.anduin-google-health = mkIf cfg.google-health.enable
      (mkExtractor "google-health" cfg.google-health.schedule);
    systemd.services.anduin-withings = mkIf cfg.withings.enable
      (mkExtractor "withings" cfg.withings.schedule);
    systemd.services.anduin-intervals = mkIf cfg.intervals.enable
      (mkExtractor "intervals" cfg.intervals.schedule);
    systemd.services.anduin-liftosaur = mkIf cfg.liftosaur.enable
      (mkExtractor "liftosaur" cfg.liftosaur.schedule);

    systemd.timers.anduin-google-health = mkIf cfg.google-health.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.google-health.schedule;
        Persistent = true;
        RandomizedDelaySec = "5m";
        Unit = "anduin-google-health.service";
      };
    };
    # ...repeat for withings, intervals, liftosaur (same shape)...
  };
}
```

Register the module in `services/default.nix`.

**Note:** the CLI binary is `anduin`; the `ExecStart` invokes
`anduin extract <source>` with no window arguments, relying on each source's
default trailing-window logic (see `src/anduin/cli.py` and the per-source
config defaults in `config.py`).

## Step 3: agenix secret

One agenix file holds all the pipeline's environment variables. Create
`secrets/anduin-env.age` containing:

```
DATABASE_URL=postgresql://anduin@127.0.0.1:5433/anduin

GOOGLE_HEALTH_CLIENT_ID=...
GOOGLE_HEALTH_CLIENT_SECRET=...

WITHINGS_CLIENT_ID=...
WITHINGS_CLIENT_SECRET=...

INTERVALS_API_KEY=...
INTERVALS_ATHLETE_ID=i95355

LIFTOSAUR_API_KEY=...
```

Declare ACL in `secrets/secrets.nix`:

```nix
"anduin-env.age".publicKeys = [
  osgiliathHost
  osgiliathUser   # so you can `agenix -e` from osgiliath
];
```

`agenix -e secrets/anduin-env.age` to author it. Wire it in
`hosts/osgiliath/services.nix`:

```nix
age.secrets.anduin-env = {
  file = ../../secrets/anduin-env.age;
  owner = "anduin";
  group = "anduin";
};
```

## Step 4: enable both modules on osgiliath

In `hosts/osgiliath/services.nix`, inside `agindin.services`:

```nix
anduin-postgres.enable = true;

anduin = {
  enable = true;
  environmentFile = config.age.secrets.anduin-env.path;
  google-health.enable = true;
  withings.enable      = true;
  intervals.enable     = true;
  liftosaur.enable     = true;
};
```

If `globalVars.ports` is the canonical place for ports in this config (it is —
see `common/variables.nix`), add `anduinPostgres = 5433;` to the ports attrset
and reference `globalVars.ports.anduinPostgres` from
`agindin.services.anduin-postgres.hostPort` instead of hardcoding.

## Step 5: deploy order

The first deploy will:
1. Build the container with postgres+timescale.
2. Start it; postgres init creates `anduin` DB and roles.
3. Run `anduin-db-migrate.service`, which applies the 8 migrations.
4. Try to start the four extractor services on their timers. Three of them
   (intervals, liftosaur, withings post-seed) will succeed; google-health and
   withings will **fail until you seed OAuth tokens**.

That failure-until-seeded is expected. Don't worry about it; just deploy.

```
colmena apply --on osgiliath
```

Then verify:

```
ssh osgiliath sudo systemctl status container@anduin-postgres.service
ssh osgiliath sudo nixos-container root-login anduin-postgres -c \
  'sudo -u postgres psql anduin -c "\dx"'           # timescaledb listed
ssh osgiliath sudo systemctl start anduin-db-migrate.service
ssh osgiliath sudo journalctl -u anduin-db-migrate.service -n 50
```

## Step 6: seed OAuth tokens (one-time, interactive)

Two sources use OAuth2: `google-health` and `withings`. Each needs a one-time
interactive seed because the user has to click "Allow" in a browser.

You'll need:
- A Google Cloud OAuth client configured for the Fitness API scopes listed in
  `src/anduin/oauth_flow.py::GOOGLE_HEALTH_SCOPES`. Set the authorized
  redirect URI to `http://127.0.0.1:8765/` (the default `--port`).
- A Withings developer app with redirect URI `http://127.0.0.1:8765/`.

Seeding runs as the `anduin` system user so the token file lands at the right
path with the right ownership. From your laptop:

```
ssh -L 8765:127.0.0.1:8765 osgiliath
sudo -u anduin \
  STATE_DIRECTORY=/var/lib/anduin/state \
  ANDUIN_CONFIG=$(systemctl show anduin-db-migrate.service -p Environment | sed -n 's/.*ANDUIN_CONFIG=\([^ ]*\).*/\1/p') \
  /run/current-system/sw/bin/env $(cat /run/agenix/anduin-env | xargs) \
  /nix/store/<hash>-anduin-0.1.0/bin/anduin auth google-health
```

That's clunky. The smoother path: add a small helper to `services/anduin.nix`:

```nix
environment.systemPackages = [
  (pkgs.writeShellScriptBin "anduin-auth" ''
    if [ -z "$1" ]; then echo "usage: anduin-auth <google-health|withings>"; exit 2; fi
    exec sudo -u ${cfg.user} \
      env $(cat ${cfg.environmentFile} | xargs) \
      STATE_DIRECTORY=/var/lib/anduin/state \
      ANDUIN_CONFIG=${configJson} \
      ${cfg.package}/bin/anduin auth "$1"
  '')
];
```

Then on osgiliath: `sudo anduin-auth google-health` and follow the printed URL
through your SSH-forwarded localhost. Repeat for `withings`.

Token files land at `/var/lib/anduin/state/<source>/token.json` (mode 0600).
The extractors will pick them up on the next timer fire and refresh them
in-place when they expire.

## Step 7: verify end-to-end

```
ssh osgiliath
sudo systemctl start anduin-intervals.service
sudo journalctl -u anduin-intervals.service -n 80
# Expect: "intervals: N activities in YYYY-MM-DD..YYYY-MM-DD" and a
# SourceResult summary with row counts.

sudo nixos-container root-login anduin-postgres -c \
  'sudo -u postgres psql anduin -c "
    SELECT source, count(*), min(valid_from), max(valid_from)
      FROM raw.samples GROUP BY source;
    SELECT count(*) FROM raw.activities;
    SELECT count(*) FROM raw.activity_streams;
    SELECT count(*) FROM raw.strength_sets;
    SELECT * FROM canonical.precedence_rules ORDER BY metric, rank LIMIT 20;
  "'
```

Restatement test — re-run the same extractor immediately. Row counts in
`raw.samples` should not grow; `raw.restatements` should only grow if a source
actually changed a payload between the two runs.

## Backup verification

```
ssh osgiliath sudo systemctl start anduin-postgres-backup.service
ls -lh /var/backup/postgres-anduin/anduin.dump
restic snapshots --tag osgiliath | head      # the dump dir is covered
```

## Things to verify before deploy

- The Google Health endpoints in `src/anduin/sources/google_health.py` are
  written against the documented Fitbit-via-Google paths. Confirm those paths
  and scopes against Google's current Health API docs before relying on the
  data; the OAuth + persistence machinery does not change if a URL needs to
  swap.
- The Liftosaur API endpoint (`/api/storage?apikey=...`) is what the Liftosaur
  web app uses. If you want to be defensive, set up a `liftosaur` API token
  rather than the storage-export apikey and adjust `sources/liftosaur.py`.
- The 192.168.100.0/24 subnet for the container bridge is unused on osgiliath —
  grep the rest of `hosts/osgiliath/` for any conflicts before committing.
- `nixos-container` requires the host's `boot.enableContainers = true`
  (usually the default). Confirm on osgiliath.

## Things that are intentionally out of scope

- No analyzer/UI. The plan stops at raw + canonical-view layer. A future
  service can connect as `anduin_ro` via the container's bridge IP.
- No metrics export. If you want to see ingestion counters in Grafana, add a
  `prometheus-anduin-exporter` later; the SourceResult logs already land in
  Loki via the existing alloy → loki shipping path.

{
  description = "anduin — personal health data pipeline (Google Health / Withings / intervals.icu / Liftosaur → TimescaleDB).";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      mkPackage =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "anduin";
          version = "0.1.0";
          pyproject = true;

          src = ./.;

          build-system = [ pkgs.python3Packages.hatchling ];

          dependencies = with pkgs.python3Packages; [
            httpx
            psycopg
            pydantic
            pydantic-settings
          ];

          nativeCheckInputs = with pkgs.python3Packages; [
            pytestCheckHook
            respx
            freezegun
          ];

          pythonImportsCheck = [ "anduin" ];
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          anduin = mkPackage pkgs;
        in
        {
          inherit anduin;
          default = anduin;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (
                ps: with ps; [
                  httpx
                  psycopg
                  pydantic
                  pydantic-settings
                  pytest
                  respx
                  freezegun
                ]
              ))
              pkgs.ruff
              pkgs.postgresql_16
            ];
          };
        }
      );

      checks = forAllSystems (system: { build = self.packages.${system}.anduin; });
    };
}

{
  description = "Replicate: restore SQL Server databases from VHD images into a Windows VM you own";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: rec {
        replicate = pkgs.python3Packages.buildPythonApplication {
          pname = "replicate";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            pygobject3
            pycairo
            python-tds
          ];
          nativeBuildInputs = with pkgs; [
            wrapGAppsHook4
            gobject-introspection
          ];
          buildInputs = with pkgs; [
            gtk4
            libadwaita
            libsecret
          ];

          # Tools the app calls at run time.
          makeWrapperArgs = [
            "--prefix PATH : ${pkgs.lib.makeBinPath (with pkgs; [ libvirt qemu-utils xorriso virt-viewer ])}"
          ];

          postInstall = ''
            install -Dm644 data/io.github.tahuffman1s.Replicate.desktop -t $out/share/applications
            install -Dm644 data/io.github.tahuffman1s.Replicate.metainfo.xml -t $out/share/metainfo
            install -Dm644 data/icons/io.github.tahuffman1s.Replicate.svg -t $out/share/icons/hicolor/scalable/apps
            install -Dm644 data/io.github.tahuffman1s.Replicate-symbolic.svg -t $out/share/icons/hicolor/symbolic/apps
          '';

          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook pkgs.qemu-utils ];

          meta = with pkgs.lib; {
            description = "Restore SQL Server databases from VHD images into a Windows VM you own";
            homepage = "https://github.com/tahuffman1s/Replicate";
            license = licenses.gpl3Plus;
            mainProgram = "replicate";
            platforms = platforms.linux;
          };
        };
        default = replicate;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            (python3.withPackages (ps: with ps; [ pygobject3 pycairo python-tds pytest ]))
            gtk4
            libadwaita
            libsecret
            gobject-introspection
            libvirt
            qemu-utils
            xorriso
            virt-viewer
            appstream
            desktop-file-utils
          ];
          shellHook = ''
            export PYTHONPATH=$PWD:$PYTHONPATH
            echo "Replicate dev shell: run 'python -m replicate'"
          '';
        };
      });

      # Example: a NixOS module users can import to get libvirt the way Replicate
      # expects it (system URI, swtpm for Windows 11, the user in the libvirt group).
      nixosModules.host = { config, lib, pkgs, ... }: {
        options.programs.replicate = {
          enable = lib.mkEnableOption "Replicate and a libvirt setup for its Windows VM";
          users = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            description = "Users allowed to manage the VM (added to libvirtd and kvm).";
          };
        };
        config = lib.mkIf config.programs.replicate.enable {
          environment.systemPackages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.replicate pkgs.virt-viewer ];
          virtualisation.libvirtd = {
            enable = true;
            qemu = {
              package = pkgs.qemu_kvm;
              swtpm.enable = true;
              ovmf.enable = true;
              ovmf.packages = [ pkgs.OVMFFull.fd ];
            };
          };
          users.users = lib.genAttrs config.programs.replicate.users (_: {
            extraGroups = [ "libvirtd" "kvm" ];
          });
        };
      };
    };
}

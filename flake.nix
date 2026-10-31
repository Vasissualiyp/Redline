{
  description = "Redline: review AI-suggested LaTeX `changes` edits from nvim, tmux and MCP";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Only the overleaf-sync derivation, not the papers repo it lives in.
    # After editing it there: nix flake update overleaf-sync
    overleaf-sync = {
      url = "file+file:///home/vasilii/Software/Overleaf-nix-flake/overleaf-sync.nix";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, flake-utils, overleaf-sync }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python314;
        pyDeps = ps: with ps; [ mcp pillow ];

        # Runtime tools the CLI shells out to.
        # mupdf: mutool rasterises pages 10-20x faster than poppler (poppler stays for
        # pdftotext, pdfinfo and as the fallback renderer)
        runtimeTools = with pkgs; [ poppler-utils mupdf git tmux ];

        redline = python.pkgs.buildPythonApplication {
          pname = "redline";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ python.pkgs.setuptools ];
          dependencies = pyDeps python.pkgs;
          nativeCheckInputs = [ python.pkgs.pytestCheckHook pkgs.git ];
          # Compile tests need a TeX install; they skip themselves without latexmk.
          makeWrapperArgs = [ "--suffix" "PATH" ":" (pkgs.lib.makeBinPath runtimeTools) ];
          postInstall = ''
            mkdir -p $out/share/nvim
            cp -r nvim $out/share/nvim/redline
          '';
        };

        ols = pkgs.callPackage overleaf-sync { };
        # bin only, so ols' python3 dependencies stay off PYTHONPATH
        ols-bin = pkgs.runCommand "ols-bin" { } ''
          mkdir -p $out/bin
          ln -s ${ols}/bin/ols $out/bin/ols
        '';

        redline-nvim = pkgs.vimUtils.buildVimPlugin {
          pname = "redline-nvim";
          version = "0.1.0";
          src = ./nvim;
        };
      in {
        packages = {
          default = redline;
          overleaf-sync = ols;
          inherit redline redline-nvim;
        };

        apps.default = flake-utils.lib.mkApp { drv = redline; };

        # Paper workspace: `nix develop ~/Software/Redline#paper`, then `redline open`.
        # TeX (latexmk/synctex), nvim, kitty and zotero-mcp come from the system.
        devShells.paper = pkgs.mkShell {
          packages = [ redline ols-bin ] ++ runtimeTools;
          shellHook = ''
            unset PYTHONPATH
          '';
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: pyDeps ps ++ [ ps.pytest ]))
          ] ++ runtimeTools;
          shellHook = ''
            export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      });
}

# overleaf-sync (ols) with local patches: a requests-based CLI login instead of the
# QtWebEngine one (crashes under Nix), a browser User-Agent, and ~/.olauth as cookie file.
{ python3 }:
let
  python = python3;

  yaspin-2 = python.pkgs.buildPythonPackage rec {
    pname = "yaspin";
    version = "2.5.0";
    format = "pyproject";
    src = python.pkgs.fetchPypi {
      inherit pname version;
      sha256 = "f96ab3b5c42e1eaa6af3193508082309d9dc43f6963339f9aa606003ee8d7e63";
    };
    nativeBuildInputs = with python.pkgs; [ poetry-core pythonRelaxDepsHook ];
    pythonRelaxDeps = [ "termcolor" ];
    propagatedBuildInputs = with python.pkgs; [ termcolor ];
    doCheck = false;
  };
in
python.pkgs.buildPythonApplication rec {
  pname = "overleaf-sync";
  version = "1.2.0";
  format = "setuptools";

  src = python.pkgs.fetchPypi {
    inherit pname version;
    sha256 = "94ace0ffc902dadc31934dedc5f11cbbbd5b58efb85bb1fce10020d4eca7a3ec";
  };

  propagatedBuildInputs = with python.pkgs; [
    requests
    beautifulsoup4
    python-dateutil
    click
    websocket-client
    yaspin-2
  ];

  postPatch = ''
    # relax strict version pins in setup.cfg/setup.py
    sed -i \
      -e 's/beautifulsoup4 == 4\.11\.1/beautifulsoup4/' \
      -e 's/yaspin == 2\.\*/yaspin/' \
      -e 's/python-dateutil~=2\.8\.1/python-dateutil/' \
      -e 's/socketIO-client == 0\.5\.7\.2/websocket-client/' \
      -e 's/PySide6 == 6\.\*//' \
      setup.cfg setup.py 2>/dev/null || true

    # Store the auth cookie in ~/.olauth so it is found from any directory.
    sed -i 's|default=".olauth"|default=os.path.expanduser("~/.olauth")|g' olsync/olsync.py

    # Plain requests-based login (no QtWebEngine) and a client sending a browser
    # User-Agent and a raw Cookie header (see ./overleaf-sync/).
    cp ${./overleaf-sync/olbrowserlogin.py} olsync/olbrowserlogin.py
    cp ${./overleaf-sync/olclient.py} olsync/olclient.py

    # Ask for credentials before execute_action() starts the yaspin spinner,
    # which hijacks the terminal so input() is unusable inside it.
    substituteInPlace olsync/olsync.py \
      --replace-fail \
        'click.clear()
        execute_action(lambda: login_handler(cookie_path), "Login",' \
        'click.clear()
        olbrowserlogin.prompt_credentials()
        execute_action(lambda: login_handler(cookie_path), "Login",'
  '';

  doCheck = false;
}

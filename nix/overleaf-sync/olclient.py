"""Overleaf Client"""

import requests as reqs
from bs4 import BeautifulSoup
import json
import uuid
import websocket

LOGIN_URL = "https://www.overleaf.com/login"
PROJECT_URL = "https://www.overleaf.com/project"
DASHBOARD_URL = "https://www.overleaf.com/project"
PROJECT_PAGE_URL = "https://www.overleaf.com/project/{}"
SOCKET_ID_URL = "https://www.overleaf.com/socket.io/1/?projectId={}"
SOCKET_URL = "wss://www.overleaf.com/socket.io/1/websocket/{}?projectId={}"
DOWNLOAD_URL = "https://www.overleaf.com/project/{}/download/zip"
UPLOAD_URL = "https://www.overleaf.com/project/{}/upload"
FOLDER_URL = "https://www.overleaf.com/project/{}/folder"
DELETE_URL = "https://www.overleaf.com/project/{}/doc/{}"
COMPILE_URL = "https://www.overleaf.com/project/{}/compile?enable_pdf_caching=true"
BASE_URL = "https://www.overleaf.com"
PATH_SEP = "/"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class OverleafClient(object):

    @staticmethod
    def filter_projects(json_content, more_attrs=None):
        more_attrs = more_attrs or {}
        for p in json_content:
            if not p.get("archived") and not p.get("trashed"):
                if all(p.get(k) == v for k, v in more_attrs.items()):
                    yield p

    def __init__(self, cookie=None, csrf=None):
        self._cookie = cookie
        self._csrf = csrf
        # Build raw cookie string to avoid Python http.cookiejar encoding issues
        if isinstance(cookie, dict):
            self._cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
        else:
            self._cookie_str = "; ".join(f"{c.name}={c.value}" for c in (cookie or []))

    def _h(self, extra=None):
        """Headers with browser UA and raw Cookie string."""
        h = {"User-Agent": BROWSER_UA, "Cookie": self._cookie_str}
        if extra:
            h.update(extra)
        return h

    def login(self, username, password):
        """Deprecated – kept for API compatibility."""
        get_login = reqs.get(LOGIN_URL)
        self._csrf = BeautifulSoup(get_login.content, 'html.parser').find(
            'input', {'name': '_csrf'}).get('value')
        login_json = {"_csrf": self._csrf, "email": username, "password": password}
        post_login = reqs.post(LOGIN_URL, json=login_json, cookies=get_login.cookies)
        if post_login.status_code == 200 and get_login.cookies["overleaf_session2"] != post_login.cookies["overleaf_session2"]:
            self._cookie = post_login.cookies
            self._cookie['GCLB'] = get_login.cookies['GCLB']
            projects_page = reqs.get(PROJECT_URL, cookies=self._cookie)
            self._csrf = BeautifulSoup(projects_page.content, 'html.parser').find('meta', {'name': 'ol-csrfToken'}).get('content')
            return {"cookie": self._cookie, "csrf": self._csrf}

    def _parse_projects(self, page_content):
        soup = BeautifulSoup(page_content, 'html.parser')
        meta = soup.find('meta', {'name': 'ol-prefetchedProjectsBlob'})
        if meta:
            data = json.loads(meta.get('content'))
            if 'projects' in data:
                return data['projects']
        meta = soup.find('meta', {'name': 'ol-projects'})
        if meta:
            return json.loads(meta.get('content'))
        raise Exception("Cannot find projects data — session may have expired.")

    def all_projects(self):
        page = reqs.get(PROJECT_URL, headers=self._h())
        if not page.ok:
            raise Exception(f"Failed to fetch projects: HTTP {page.status_code}")
        return list(OverleafClient.filter_projects(self._parse_projects(page.content)))

    def get_project(self, project_name):
        page = reqs.get(PROJECT_URL, headers=self._h())
        return next(OverleafClient.filter_projects(
            self._parse_projects(page.content), {"name": project_name}), None)

    def download_project(self, project_id):
        r = reqs.get(DOWNLOAD_URL.format(project_id), stream=True, headers=self._h())
        return r.content

    def create_folder(self, project_id, parent_folder_id, folder_name):
        params = {"parent_folder_id": parent_folder_id, "name": folder_name}
        extra = {"X-Csrf-Token": self._csrf}
        r = reqs.post(FOLDER_URL.format(project_id), headers=self._h(extra), json=params)
        if r.ok:
            return json.loads(r.content)
        elif r.status_code == str(400):
            return
        else:
            raise reqs.HTTPError()

    def get_project_infos(self, project_id):
        """Get detailed project infos via WebSocket (replaces broken socketIO_client approach).

        Overleaf's Socket.IO endpoint is accessed by:
        1. GET /socket.io/1/?projectId=<id>  -> returns "<socket_id>:..."
        2. WSS /socket.io/1/websocket/<socket_id>?projectId=<id>&esh=1&ssp=1
           - recv() the connection acknowledgement
           - send("joinProjectResponse") to trigger the server response
           - recv() the JSON payload containing project data
        """
        import time
        project_page_url = PROJECT_PAGE_URL.format(project_id)

        # Step 1: obtain the Socket.IO session id.
        # Use a session so response cookies (e.g. load-balancer routing) are captured.
        s = reqs.Session()
        s.headers.update({"User-Agent": BROWSER_UA})
        resp = s.get(
            SOCKET_ID_URL.format(project_id) + f"&t={int(time.time() * 1000)}",
            headers={"Cookie": self._cookie_str, "Referer": project_page_url},
        )
        resp.raise_for_status()
        socket_str = resp.text.split(":")[0]

        # Build cookie string: original session cookie + any routing cookies the
        # handshake response set (e.g. GCLB for sticky load-balancer routing).
        ws_cookie = self._cookie_str
        extra = "; ".join(f"{k}={v}" for k, v in s.cookies.items()
                          if k not in self._cookie_str)
        if extra:
            ws_cookie += "; " + extra

        # Step 2: WebSocket upgrade.
        # The server auto-emits joinProjectResponse because projectId is in the
        # query string — we must NOT send it ourselves, only recv it.
        ws = websocket.WebSocket()
        ws.connect(
            SOCKET_URL.format(socket_str, project_id),
            header={"User-Agent": BROWSER_UA},
            cookie=ws_cookie,
            origin="https://www.overleaf.com",
            host="www.overleaf.com",
        )
        ws.recv()  # Socket.IO connect frame "1::"

        # Loop: skip heartbeat / other frames until joinProjectResponse arrives
        response = ""
        for _ in range(20):
            response = ws.recv()
            if '"joinProjectResponse"' in response:
                break
        ws.close()

        idx = response.find("{")
        project_info = json.loads(response[idx:])['args'][0]['project']
        return project_info

    def _get_csrf_token(self, project_id):
        """Fetch a fresh CSRF token from the project page (required before uploads)."""
        resp = reqs.get(PROJECT_PAGE_URL.format(project_id), headers=self._h())
        if resp.ok:
            soup = BeautifulSoup(resp.content, 'html.parser')
            meta = soup.find('meta', {'name': 'ol-csrfToken'})
            if meta:
                self._csrf = meta.get('content')
        return self._csrf

    def upload_file(self, project_id, project_infos, file_name, file_size, file):
        # Always refresh CSRF from the project page — the stored token may be stale.
        csrf = self._get_csrf_token(project_id)

        folder_id = project_infos['rootFolder'][0]['_id']
        if PATH_SEP in file_name:
            local_folders = file_name.split(PATH_SEP)[:-1]
            current_overleaf_folder = project_infos['rootFolder'][0]['folders']
            for local_folder in local_folders:
                exists_on_remote = False
                for remote_folder in current_overleaf_folder:
                    if local_folder.lower() == remote_folder['name'].lower():
                        exists_on_remote = True
                        folder_id = remote_folder['_id']
                        current_overleaf_folder = remote_folder['folders']
                        break
                if not exists_on_remote:
                    new_folder = self.create_folder(project_id, folder_id, local_folder)
                    current_overleaf_folder.append(new_folder)
                    folder_id = new_folder['_id']
                    current_overleaf_folder = new_folder['folders']

        # Use just the base filename — the folder is addressed via folder_id.
        base_name = file_name.split(PATH_SEP)[-1]

        def _do_upload():
            file.seek(0)
            # Overleaf's current upload API (2024+):
            #   folder_id goes in the query string, not the form body.
            #   Form fields: relativePath="null", name=<basename>, type=<mime>.
            #   The Fine Uploader qqfilename/qquuid fields are no longer used.
            return reqs.post(
                UPLOAD_URL.format(project_id),
                headers=self._h({
                    "x-csrf-token": csrf,
                    "Referer": PROJECT_PAGE_URL.format(project_id),
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                }),
                params={"folder_id": folder_id},
                data={
                    "relativePath": "null",
                    "name": base_name,
                    "type": "application/octet-stream",
                },
                files={"qqfile": (base_name, file, "application/octet-stream")},
            )

        r = _do_upload()

        if r.status_code == 422:
            # File already exists — delete it first, then re-upload.
            self.delete_file(project_id, project_infos, file_name)
            r = _do_upload()

        if not r.ok:
            raise Exception(f"Upload failed HTTP {r.status_code}: {r.text[:300]}")
        result = json.loads(r.content)
        if not result.get("success"):
            raise Exception(f"Upload rejected by Overleaf: {r.text[:300]}")
        return True

    def _find_entity(self, project_infos, file_name):
        """Find a doc or fileRef by name, searching recursively through folders."""
        base = file_name.split(PATH_SEP)[-1]
        parts = file_name.split(PATH_SEP)[:-1]
        folder = project_infos['rootFolder'][0]
        for part in parts:
            folder = next((f for f in folder.get('folders', [])
                           if f['name'].lower() == part.lower()), None)
            if folder is None:
                return None
        return (next((v for v in folder.get('docs', []) if v['name'] == base), None)
                or next((v for v in folder.get('fileRefs', []) if v['name'] == base), None))

    def delete_file(self, project_id, project_infos, file_name):
        entity = self._find_entity(project_infos, file_name)
        if entity is None:
            return False
        r = reqs.delete(
            DELETE_URL.format(project_id, entity['_id']),
            headers=self._h({"X-Csrf-Token": self._csrf}),
            json={},
        )
        return r.status_code == 204

    def download_pdf(self, project_id):
        extra = {"X-Csrf-Token": self._csrf}
        body = {
            "check": "silent",
            "draft": False,
            "incrementalCompilesEnabled": True,
            "rootDoc_id": "",
            "stopOnFirstError": False,
        }
        r = reqs.post(COMPILE_URL.format(project_id), headers=self._h(extra), json=body)
        if not r.ok:
            raise reqs.HTTPError()
        compile_result = json.loads(r.content)
        if compile_result["status"] != "success":
            raise reqs.HTTPError()
        pdf_file = next(v for v in compile_result['outputFiles'] if v['type'] == 'pdf')
        download_req = reqs.get(BASE_URL + pdf_file['url'], headers=self._h(extra))
        if download_req.ok:
            return pdf_file['path'], download_req.content
        return None

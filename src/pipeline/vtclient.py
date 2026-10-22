"""VTScada REST client -- the delivery oracle.

Two jobs:

1. `reachable()` -- is the VTScada historian answering? The bridge publishes
   nothing while the answer is no, so samples accumulate in the queue instead of
   being published into the void.
2. `history()` -- read a tag's history back out, so a delivery can be *proved*
   rather than assumed. MQTT publish success and QoS 1 acks say nothing about
   whether VTScada stored the data.

The endpoint is VTScada's own REST interface:

    GET {base}/{realm}/REST/SQLQuery?query=<url-encoded SQL>

`SQLQuery` is fixed and case-sensitive -- an invented function name answers
`{"errorMessage":"Unknown REST Function"}` with HTTP 200, so check for it.

The `History` table is WIDE: one `Timestamp` column (UTC epoch seconds) plus one
column per logged variable, named `'<Full\\Tag\\Path>:Value'`. There is no
Name/Time/Value column, so a tag path is only usable as a quoted column literal.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import settings

log = logging.getLogger("vtscada")


class VTScadaError(RuntimeError):
    """The REST interface answered, but with an error."""


class VTScadaUnreachable(VTScadaError):
    """The REST interface did not answer at all (down, or network)."""


class VTScadaAuthError(VTScadaError):
    """The REST interface answered 401/403: credentials or permissions are wrong.

    Distinct from VTScadaUnreachable on purpose. A wrong password used to look
    exactly like a dead historian -- the bridge sat in STANDBY and the queue grew
    while the operator chased a network fault that did not exist (audit F3).
    """


class VTScadaClient:
    def __init__(self, base_url=None, realm=None, user=None, password=None, timeout=None):
        self.base_url = (base_url or settings.VTS_REST_URL).rstrip("/")
        self.realm = realm or settings.VTS_REST_REALM
        self.user = user or settings.VTS_REST_USER
        self.password = password if password is not None else settings.VTS_REST_PASSWORD
        self.timeout = timeout or settings.VTS_REST_TIMEOUT_SEC

    # ------------------------------------------------------------------ transport
    def _request(self, params: dict) -> dict:
        url = f"{self.base_url}/{self.realm}/REST/SQLQuery?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        # Header auth rather than userinfo in the URL: keeps credentials out of
        # logs and stops a browser replaying them from the URL.
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        req.add_header("X-VTScadaAuth", token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # HTTPError is a URLError subclass, so it must be caught first or a
            # 401 is misreported as "unreachable".
            if e.code in (401, 403):
                raise VTScadaAuthError(
                    f"{self.base_url}: HTTP {e.code} (check VTS_REST_USER / "
                    f"VTS_REST_PASSWORD and the account's Remote Data Access rights)"
                ) from e
            raise VTScadaError(f"{self.base_url}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise VTScadaUnreachable(f"{self.base_url}: {e}") from e
        try:
            data = json.loads(body)
        except ValueError as e:
            raise VTScadaError(f"unparseable response: {body[:200]!r}") from e
        if data.get("errorMessage"):
            raise VTScadaError(str(data["errorMessage"]))
        if data.get("returnCode") not in (0, None):
            raise VTScadaError(f"returnCode={data.get('returnCode')}: {data.get('errorMessage')}")
        return data

    def sql(self, query: str) -> list[list]:
        """Run SQL and return the rows."""
        data = self._request({"query": query})
        results = data.get("results") or {}
        return results.get("values") or []

    # ------------------------------------------------------------------ queries
    # States returned by probe(); deploy/pipeline prints these verbatim.
    OK = "ok"
    UNREACHABLE = "unreachable"
    CREDENTIALS = "credentials-rejected"
    NOT_PERMITTED = "not-permitted"

    def probe(self) -> tuple[str, str]:
        """Classify the REST interface without raising.

        Separating "credentials rejected" from "unreachable" is the difference
        between fixing an account and chasing a network fault that does not
        exist (audit F3).
        """
        try:
            self._request({"query": "GET Tables"})
            return self.OK, "ok"
        except VTScadaAuthError as e:
            return self.CREDENTIALS, str(e)
        except VTScadaUnreachable as e:
            return self.UNREACHABLE, str(e)
        except VTScadaError as e:
            return self.NOT_PERMITTED, str(e)

    def reachable(self) -> tuple[bool, str]:
        """(is it answering, detail). Never raises -- this gates delivery."""
        state, detail = self.probe()
        return state == self.OK, detail

    def tables(self) -> list[str]:
        """Table names from `GET Tables`.

        The response is a result set with fieldNames
        [TABLE_CAT, TABLE_SCHEM, TABLE_NAME, TABLE_TYPE, REMARKS], so the name is
        located by column rather than by position.
        """
        data = self._request({"query": "GET Tables"})
        results = data.get("results") or {}
        names = results.get("fieldNames") or []
        try:
            idx = [n.upper() for n in names].index("TABLE_NAME")
        except ValueError:
            idx = 2
        out = []
        for row in results.get("values") or []:
            if isinstance(row, (list, tuple)) and len(row) > idx and row[idx] is not None:
                out.append(str(row[idx]))
        return out

    def tag_column(self, tag: str) -> str:
        """The History column name for a tag: <root>\\<group>\\<edge>\\<tag>:Value."""
        return "%s\\%s\\%s\\%s:Value" % (
            settings.VTS_TAG_ROOT, settings.VTS_GROUP, settings.VTS_EDGE, tag)

    # VTScada answers with at most SQLQueryMaxRows rows (10 000 by default) and
    # expects the caller to follow nextPageToken. Without this the history read
    # truncates silently on a long window -- which makes the reconcile tool report
    # rows as missing when they are in fact in the historian.
    MAX_PAGES = 500

    def history(self, tag: str, t0: float, t1: float) -> list[tuple[float, float | None]]:
        """[(timestamp, value)] for one tag over [t0, t1] (epoch seconds).

        Follows the REST interface's pagination, so the result is the whole
        window rather than the first page of it.
        """
        col = self.tag_column(tag).replace("'", "''")
        query = (
            f"SELECT Timestamp, '{col}' FROM History "
            f"WHERE Timestamp >= {float(t0)!r} AND Timestamp <= {float(t1)!r} "
            "ORDER BY Timestamp"
        )
        raw: list = []
        params: dict = {"query": query}
        for _ in range(self.MAX_PAGES):
            data = self._request(params)
            raw.extend((data.get("results") or {}).get("values") or [])
            token = data.get("nextPageToken")
            if not token:
                break
            params = {"query": query, "PageToken": token}
        else:
            raise VTScadaError(
                f"reading history for {tag} did not finish after {self.MAX_PAGES} "
                "pages -- narrow the window")
        out = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                out.append((round(float(row[0]), 3), row[1]))
            except (TypeError, ValueError):
                continue
        return out

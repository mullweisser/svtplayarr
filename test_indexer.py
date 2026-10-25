import os
import re
import tempfile
import unittest
from io import BytesIO

_tmp = tempfile.mkdtemp()
os.environ["CONFIG_DIR"] = _tmp
os.environ["JOBS_DB"] = os.path.join(_tmp, "jobs.sqlite")
os.environ["INDEXER_API_KEY"] = "test-key"
os.environ["DOWNLOAD_ROOT"] = os.path.join(_tmp, "downloads")
os.environ["SVTPLAYARR_DISABLE_WORKER"] = "1"

import app as app_module
from catalog import Catalog
from downloader import build_svtplay_dl_command
from newznab import build_nzb, token_from_nzb
from sabnzbd import process_one

KEY = "test-key"


class FakeCatalog:
    def __init__(self):
        self.tv = []
        self.movies = []
        self.sample_tv_hits = []
        self.sample_movie_hits = []

    def search_tv(self, query, season=None, episode=None):
        return list(self.tv)

    def search_movies(self, query, year=None):
        return list(self.movies)

    def sample_tv(self, limit=8):
        return list(self.sample_tv_hits)[:limit]

    def sample_movies(self, limit=8):
        return list(self.sample_movie_hits)[:limit]


def series_payload(name, episodes):
    return {
        "data": {
            "searchPage": {
                "flat": {
                    "hits": [{
                        "teaser": {
                            "item": {
                                "__typename": "TvSeries",
                                "name": name,
                                "seasons": [{
                                    "number": 1,
                                    "episodes": episodes,
                                }],
                            }
                        }
                    }]
                }
            }
        }
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class IndexerApiTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FakeCatalog()
        app_module.app.config["CATALOG"] = self.catalog
        self.client = app_module.app.test_client()
        store = app_module.app.config["STORE"]
        with store._session() as connection:
            connection.execute("DELETE FROM jobs")
            connection.execute("DELETE FROM releases")

    def test_caps_lists_tv_and_movies(self):
        response = self.client.get("/newznab/api", query_string={"t": "caps", "apikey": KEY})
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('tv-search available="yes" supportedParams="q,season,ep"', body)
        self.assertIn('movie-search available="yes" supportedParams="q"', body)
        self.assertIn('id="5000"', body)
        self.assertIn('id="2000"', body)

    def test_missing_api_key_is_rejected(self):
        caps = self.client.get("/newznab/api", query_string={"t": "caps"})
        self.assertEqual(caps.status_code, 401)
        version = self.client.get("/sabnzbd/api", query_string={"mode": "version"})
        self.assertEqual(version.status_code, 401)

    def test_tv_search_returns_nzb_enclosure(self):
        self.catalog.tv = [{
            "kind": "tv",
            "series": "Bonusfamiljen",
            "season": 1,
            "episode": 1,
            "url": "https://www.svtplay.se/video/Kk92M7e/bonusfamiljen/avsnitt-1",
            "confident": True,
        }]
        response = self.client.get("/newznab/api", query_string={
            "t": "tvsearch",
            "q": "Bonusfamiljen",
            "season": 1,
            "ep": 1,
            "apikey": KEY,
        })
        body = response.get_data(as_text=True)
        self.assertIn("Bonusfamiljen.S01E01.WEB-DL.SVT", body)
        self.assertIn("application/x-nzb", body)
        self.assertIn("t=get", body)

    def test_movie_search_uses_catalogue_title_and_year(self):
        self.catalog.movies = [{
            "kind": "movie",
            "name": "Filmen om Siw",
            "year": 2025,
            "url": "https://www.svtplay.se/video/KRo2rwM/filmen-om-siw",
        }]
        response = self.client.get("/newznab/api", query_string={
            "t": "search",
            "cat": "2000,2040",
            "q": "Filmen om Siw",
            "year": 2025,
            "apikey": KEY,
        })
        body = response.get_data(as_text=True)
        self.assertIn("Filmen.om.Siw.2025.WEB-DL.SVT", body)
        self.assertIn('value="2000"', body)
        self.assertNotIn("S01E", body)

    def test_empty_query_returns_a_feed(self):
        self.catalog.sample_movie_hits = [{
            "kind": "movie",
            "name": "Filmen om Siw",
            "year": 2025,
            "url": "https://www.svtplay.se/video/KRo2rwM/filmen-om-siw",
        }]
        response = self.client.get("/newznab/api", query_string={"t": "movie", "apikey": KEY})
        self.assertIn("<item>", response.get_data(as_text=True))

    def test_get_nzb_round_trips_through_addfile(self):
        self.catalog.tv = [{
            "kind": "tv",
            "series": "Bonusfamiljen",
            "season": 1,
            "episode": 1,
            "url": "https://www.svtplay.se/video/Kk92M7e/bonusfamiljen/avsnitt-1",
            "confident": True,
        }]
        search = self.client.get("/newznab/api", query_string={
            "t": "tvsearch", "q": "Bonusfamiljen", "season": 1, "ep": 1, "apikey": KEY,
        })
        match = re.search(r"id=([0-9a-f]{32})", search.get_data(as_text=True))
        self.assertIsNotNone(match)
        token = match.group(1)
        nzb = self.client.get("/newznab/api", query_string={"t": "get", "id": token, "apikey": KEY})
        self.assertEqual(nzb.mimetype, "application/x-nzb")
        self.assertEqual(token_from_nzb(nzb.data), token)

        added = self.client.post(
            "/sabnzbd/api",
            query_string={"mode": "addfile", "apikey": KEY, "cat": "tv", "output": "json"},
            data={"name": (BytesIO(nzb.data), "release.nzb")},
            content_type="multipart/form-data",
        )
        self.assertTrue(added.get_json()["status"])
        nzo_id = added.get_json()["nzo_ids"][0]

        queue = self.client.get("/sabnzbd/api", query_string={
            "mode": "queue", "apikey": KEY, "category": "tv", "output": "json",
        })
        slots = queue.get_json()["queue"]["slots"]
        self.assertEqual(slots[0]["nzo_id"], nzo_id)
        self.assertEqual(slots[0]["status"], "Queued")
        self.assertEqual(slots[0]["cat"], "tv")

        storage = os.path.join(_tmp, "downloads", "tv", "Bonusfamiljen.S01E01.WEB-DL.SVT")
        process_one(app_module.app.config["STORE"], lambda job: storage)
        history = self.client.get("/sabnzbd/api", query_string={
            "mode": "history", "apikey": KEY, "category": "tv", "output": "json",
        })
        slot = history.get_json()["history"]["slots"][0]
        self.assertEqual(slot["status"], "Completed")
        self.assertEqual(slot["storage"], storage)
        self.assertEqual(slot["fail_message"], "")

    def test_sabnzbd_version_config_and_failure_retry(self):
        version = self.client.get("/sabnzbd/api", query_string={"mode": "version", "apikey": KEY})
        self.assertEqual(version.get_json()["version"], "4.3.0")
        config = self.client.get("/sabnzbd/api", query_string={"mode": "get_config", "apikey": KEY})
        body = config.get_json()["config"]
        self.assertEqual(body["misc"]["history_retention_option"], "all")
        names = {category["name"]: category["dir"] for category in body["categories"]}
        self.assertEqual(names["tv"], "tv")
        self.assertEqual(names["movies"], "movies")
        status = self.client.get("/sabnzbd/api", query_string={"mode": "fullstatus", "apikey": KEY})
        self.assertEqual(status.get_json()["status"]["completedir"], os.environ["DOWNLOAD_ROOT"])

        store = app_module.app.config["STORE"]
        token = store.save_release("Show.S01E02.WEB-DL.SVT", "https://www.svtplay.se/video/abc", "tv", 10)
        nzb = build_nzb(token, "Show.S01E02.WEB-DL.SVT").encode()
        added = self.client.post(
            "/sabnzbd/api",
            query_string={"mode": "addfile", "apikey": KEY, "cat": "tv"},
            data={"name": (BytesIO(nzb), "show.nzb")},
        )
        nzo_id = added.get_json()["nzo_ids"][0]
        def fail(job):
            raise RuntimeError("download failed")

        process_one(store, fail)
        history = self.client.get("/sabnzbd/api", query_string={"mode": "history", "apikey": KEY})
        self.assertEqual(history.get_json()["history"]["slots"][0]["status"], "Failed")
        retried = self.client.get("/sabnzbd/api", query_string={
            "mode": "retry", "value": nzo_id, "apikey": KEY,
        })
        self.assertEqual(retried.get_json()["id"], nzo_id)
        queue = self.client.get("/sabnzbd/api", query_string={"mode": "queue", "apikey": KEY})
        self.assertEqual(queue.get_json()["queue"]["slots"][0]["status"], "Queued")
        removed = self.client.get("/sabnzbd/api", query_string={
            "mode": "queue", "name": "delete", "value": nzo_id, "apikey": KEY,
        })
        self.assertTrue(removed.get_json()["status"])
        queue = self.client.get("/sabnzbd/api", query_string={"mode": "queue", "apikey": KEY})
        self.assertEqual(queue.get_json()["queue"]["slots"], [])

    def test_home_lists_both_endpoints(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertIn("/newznab", body)
        self.assertIn("/sabnzbd", body)
        self.assertIn(KEY, body)


class CatalogTests(unittest.TestCase):
    def test_tv_search_filters_episode_and_skips_unplayable_paths(self):
        payload = series_payload("Bonusfamiljen", [
            {"name": "Avsnitt 1", "number": 1, "urls": {"svtplay": "/video/one/bonusfamiljen/avsnitt-1"}},
            {"name": "Avsnitt 2", "number": 2, "urls": {"svtplay": "/bonusfamiljen"}},
            {"name": "Avsnitt 3", "number": 3, "urls": {"svtplay": "/video/three/bonusfamiljen/avsnitt-3"}},
        ])
        catalog = Catalog(session=type("S", (), {"post": lambda *a, **k: Response(payload)})())
        hits = catalog.search_tv("Bonusfamiljen", 1, 1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["episode"], 1)
        self.assertEqual(hits[0]["url"], "https://www.svtplay.se/video/one/bonusfamiljen/avsnitt-1")
        self.assertTrue(hits[0]["confident"])

    def test_ambiguous_series_title_returns_nothing(self):
        payload = {
            "data": {
                "searchPage": {
                    "flat": {
                        "hits": [
                            {"teaser": {"item": {
                                "__typename": "TvSeries",
                                "name": "Show One",
                                "seasons": [{"number": 1, "episodes": [
                                    {"number": 1, "urls": {"svtplay": "/video/a/show-one"}}
                                ]}],
                            }}},
                            {"teaser": {"item": {
                                "__typename": "TvSeries",
                                "name": "Show Two",
                                "seasons": [{"number": 1, "episodes": [
                                    {"number": 1, "urls": {"svtplay": "/video/b/show-two"}}
                                ]}],
                            }}},
                        ]
                    }
                }
            }
        }
        catalog = Catalog(session=type("S", (), {"post": lambda *a, **k: Response(payload)})())
        self.assertEqual(catalog.search_tv("Show"), [])

    def test_movie_year_must_match(self):
        payload = {
            "data": {
                "searchPage": {
                    "flat": {
                        "hits": [{
                            "teaser": {
                                "item": {
                                    "__typename": "Single",
                                    "name": "Filmen om Siw",
                                    "productionYear": 2025,
                                    "urls": {"svtplay": "/video/KRo2rwM/filmen-om-siw"},
                                }
                            }
                        }]
                    }
                }
            }
        }
        catalog = Catalog(session=type("S", (), {"post": lambda *a, **k: Response(payload)})())
        self.assertEqual(catalog.search_movies("Filmen om Siw", 1999), [])
        hits = catalog.search_movies("Filmen om Siw", 2025)
        self.assertEqual(hits[0]["year"], 2025)
        self.assertTrue(hits[0]["url"].endswith("/filmen-om-siw"))


class CommandTests(unittest.TestCase):
    def test_grab_command_uses_release_filename(self):
        command = build_svtplay_dl_command(
            "https://www.svtplay.se/video/abc",
            "/downloads/tv/Show.S01E01.WEB-DL.SVT",
            {"svtplay_dl": {"quality": "best", "subtitle": True}},
            filename="Show.S01E01.WEB-DL.SVT",
        )
        self.assertIn("--filename", command)
        self.assertIn("Show.S01E01.WEB-DL.SVT", command)
        self.assertIn("--subtitle", command)
        self.assertEqual(command[-1], "https://www.svtplay.se/video/abc")


class LiveCatalogTests(unittest.TestCase):
    def test_bonusfamiljen_season_one_episode_one(self):
        try:
            hits = Catalog().search_tv("Bonusfamiljen", 1, 1)
        except Exception as exc:
            self.skipTest(f"SVT catalogue unavailable: {exc}")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["season"], 1)
        self.assertEqual(hits[0]["episode"], 1)
        self.assertIn("/video/", hits[0]["url"])


if __name__ == "__main__":
    unittest.main()

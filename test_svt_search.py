import unittest
from unittest.mock import MagicMock, patch

from content_matcher import ContentMatcher


def teaser(name, path, video_id, kind="TvShow"):
    return {
        "teaser": {
            "item": {
                "__typename": kind,
                "name": name,
                "videoSvtId": video_id,
                "urls": {"svtplay": path},
            }
        }
    }


def graphql_response(hits):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "data": {"searchPage": {"flat": {"hits": hits}}}
    }
    return response


class SvtSearchTests(unittest.TestCase):
    def setUp(self):
        self.matcher = ContentMatcher()

    @patch("content_matcher.requests.post")
    def test_show_uses_video_id(self, post):
        post.return_value = graphql_response([
            teaser("Rapport", "/rapport", "jLJJAqw", "TvShow"),
        ])

        results = self.matcher.search_svt("Rapport")

        self.assertEqual(results, [{
            "title": "Rapport",
            "url": "https://www.svtplay.se/video/jLJJAqw",
            "kind": "TvShow",
        }])
        self.assertEqual(post.call_args.kwargs["json"]["variables"]["query"], "Rapport")

    @patch("content_matcher.requests.post")
    def test_video_path_is_kept(self, post):
        post.return_value = graphql_response([
            teaser(
                "Extra Rapport - mordet på Olof Palme",
                "/video/jMdmPWB/extra-rapport-mordet-pa-olof-palme",
                "ja4mQGE",
                "Single",
            ),
        ])

        results = self.matcher.search_svt("Extra Rapport")

        self.assertEqual(
            results[0]["url"],
            "https://www.svtplay.se/video/jMdmPWB/extra-rapport-mordet-pa-olof-palme",
        )

    def test_exact_title_outranks_partial(self):
        items = [
            {"title": "Rapport till himlen", "url": "https://www.svtplay.se/video/long", "kind": "TvSeries"},
            {"title": "Rapport", "url": "https://www.svtplay.se/video/exact", "kind": "TvShow"},
            {"title": "Extra Rapport - mordet på Olof Palme", "url": "https://www.svtplay.se/video/extra", "kind": "Single"},
        ]

        best = self.matcher.best_svt_match(items, "Rapport")

        self.assertEqual(best["url"], "https://www.svtplay.se/video/exact")

    @patch("content_matcher.requests.post")
    def test_graphql_error_returns_empty(self, post):
        post.side_effect = RuntimeError("connection failed")

        self.assertEqual(self.matcher.search_svt("Rapport"), [])

    @patch("content_matcher.requests.post")
    def test_http_error_returns_empty(self, post):
        response = MagicMock()
        response.status_code = 500
        post.return_value = response

        self.assertEqual(self.matcher.search_svt("Rapport"), [])


if __name__ == "__main__":
    unittest.main()

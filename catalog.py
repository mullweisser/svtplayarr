import logging
import re

import requests

logger = logging.getLogger(__name__)

SVT_GRAPHQL_URL = "https://contento.svt.se/graphql"
PLAYABLE_PREFIXES = ("/video/", "/klipp/", "/kanaler/")
SEARCH_QUERY = """
query Search($query: String!, $maxHits: Int) {
  searchPage(query: $query, maxHits: $maxHits) {
    flat {
      hits {
        teaser {
          item {
            __typename
            name
            ... on TvSeries {
              seasons {
                number
                episodes {
                  name
                  number
                  urls { svtplay }
                }
              }
            }
            ... on Single {
              productionYear
              urls { svtplay }
            }
          }
        }
      }
    }
  }
}
"""


def playable_url(path):
    if not path or not path.startswith(PLAYABLE_PREFIXES):
        return None
    return "https://www.svtplay.se" + path


def _fold(value):
    return (value or "").casefold().strip()


class Catalog:
    def __init__(self, session=None):
        self.session = session or requests

    def search_tv(self, query, season=None, episode=None):
        query = (query or "").strip()
        if not query:
            return []
        shows = self._series(self._search(query, 20))
        chosen, confident = self._pick_series(shows, query)
        if not chosen:
            return []
        series_name = query if confident else chosen["name"]
        hits = []
        for item in self._episodes(chosen):
            if season is not None and item["season"] != season:
                continue
            if episode is not None and item["episode"] != episode:
                continue
            hits.append({
                "kind": "tv",
                "series": series_name,
                "season": item["season"],
                "episode": item["episode"],
                "url": item["url"],
                "confident": confident,
            })
        return hits

    def search_movies(self, query, year=None):
        query = (query or "").strip()
        if not query:
            return []
        movies = self._singles(self._search(query, 20))
        chosen = self._pick_movie(movies, query, year)
        if not chosen:
            return []
        return [{
            "kind": "movie",
            "name": chosen["name"],
            "year": chosen.get("year"),
            "url": chosen["url"],
        }]

    def sample_tv(self, limit=8):
        hits = []
        for query in ("drama", "nyheter"):
            for show in self._series(self._search(query, 10)):
                for item in self._episodes(show):
                    hits.append({
                        "kind": "tv",
                        "series": show["name"],
                        "season": item["season"],
                        "episode": item["episode"],
                        "url": item["url"],
                        "confident": False,
                    })
                    if len(hits) >= limit:
                        return hits
            if hits:
                return hits
        return hits

    def sample_movies(self, limit=8):
        hits = []
        for item in self._singles(self._search("film", 15)):
            hits.append({
                "kind": "movie",
                "name": item["name"],
                "year": item.get("year"),
                "url": item["url"],
            })
            if len(hits) >= limit:
                break
        return hits

    def _search(self, query, max_hits):
        try:
            response = self.session.post(
                SVT_GRAPHQL_URL,
                json={"query": SEARCH_QUERY, "variables": {"query": query, "maxHits": max_hits}},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.error("SVT catalogue search failed: %s", exc)
            raise

        if payload.get("errors") and not payload.get("data"):
            logger.error("SVT catalogue search error: %s", payload["errors"])
            return []
        hits = (((payload.get("data") or {}).get("searchPage") or {}).get("flat") or {}).get("hits") or []
        items = []
        for hit in hits:
            item = ((hit or {}).get("teaser") or {}).get("item")
            if item and item.get("name"):
                items.append(item)
        return items

    def _series(self, items):
        shows = []
        for item in items:
            if item.get("__typename") != "TvSeries":
                continue
            shows.append({"name": item["name"], "seasons": item.get("seasons") or []})
        return shows

    def _singles(self, items):
        movies = []
        seen = set()
        for item in items:
            if item.get("__typename") != "Single":
                continue
            url = playable_url((item.get("urls") or {}).get("svtplay"))
            if not url or url in seen:
                continue
            seen.add(url)
            movies.append({
                "name": item["name"],
                "year": item.get("productionYear"),
                "url": url,
            })
        return movies

    def _episodes(self, show):
        episodes = []
        seen = set()
        for season in show.get("seasons") or []:
            season_number = season.get("number")
            if season_number is None:
                continue
            for episode in season.get("episodes") or []:
                episode_number = episode.get("number")
                url = playable_url((episode.get("urls") or {}).get("svtplay"))
                if episode_number is None or not url:
                    continue
                key = (int(season_number), int(episode_number), url)
                if key in seen:
                    continue
                seen.add(key)
                episodes.append({
                    "season": int(season_number),
                    "episode": int(episode_number),
                    "url": url,
                })
        episodes.sort(key=lambda item: (item["season"], item["episode"]))
        return episodes

    def _pick_series(self, shows, query):
        wanted = _fold(query)
        exact = [show for show in shows if _fold(show["name"]) == wanted]
        if exact:
            return exact[0], True
        contained = [
            show for show in shows
            if wanted and (wanted in _fold(show["name"]) or _fold(show["name"]) in wanted)
        ]
        if len(contained) == 1:
            return contained[0], True
        return None, False

    def _pick_movie(self, movies, query, year):
        wanted = _fold(query)
        exact = [movie for movie in movies if _fold(movie["name"]) == wanted]
        if not exact:
            contained = [
                movie for movie in movies
                if wanted and (wanted in _fold(movie["name"]) or _fold(movie["name"]) in wanted)
            ]
            if len(contained) != 1:
                return None
            exact = contained
        if year is None:
            return exact[0]
        for movie in exact:
            if movie.get("year") == year:
                return movie
        return None

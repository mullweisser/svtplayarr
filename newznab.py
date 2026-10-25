import re
from datetime import datetime, timezone
from email.utils import formatdate
from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, request

from auth import api_key_matches

bp = Blueprint("newznab", __name__)

RELEASE_SIZE = 1_400_000_000
TOKEN_IN_NZB = re.compile(r"""type=["']svtplayarr-id["']>\s*([0-9a-f]+)\s*<""")
TOKEN_ANYWHERE = re.compile(r"svtplayarr-([0-9a-f]{16,})")


def dot_name(value):
    cleaned = re.sub(r"[^\w]+", " ", value or "", flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.replace(" ", ".")


def tv_release_title(hit, query):
    name = query if hit.get("confident") and query else hit["series"]
    return f"{dot_name(name)}.S{int(hit['season']):02d}E{int(hit['episode']):02d}.WEB-DL.SVT"


def movie_release_title(hit):
    name = dot_name(hit["name"])
    if hit.get("year"):
        return f"{name}.{int(hit['year'])}.WEB-DL.SVT"
    return f"{name}.WEB-DL.SVT"


def parse_categories(raw):
    if not raw:
        return []
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    categories = []
    for value in values:
        value = str(value).strip()
        if value.isdigit():
            categories.append(int(value))
    return categories


def parse_int(raw):
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def search_kind(mode, categories):
    mode = (mode or "search").lower()
    movie_categories = [category for category in categories if 2000 <= category < 3000]
    tv_categories = [category for category in categories if 5000 <= category < 6000]
    if mode in ("movie", "movie-search"):
        return "movie"
    if mode in ("tvsearch", "tv-search"):
        return "tv"
    if movie_categories and not tv_categories:
        return "movie"
    if tv_categories and not movie_categories:
        return "tv"
    return "both"


def build_nzb(token, title):
    safe_title = escape(title)
    safe_token = escape(token)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
        "  <head>\n"
        f'    <meta type="title">{safe_title}</meta>\n'
        f'    <meta type="svtplayarr-id">{safe_token}</meta>\n'
        "  </head>\n"
        f'  <file poster="svtplayarr@local" date="0" subject="{safe_title} svtplayarr-{safe_token}">\n'
        "    <groups><group>alt.binaries.svt</group></groups>\n"
        "    <segments>\n"
        f'      <segment bytes="1" number="1">svtplayarr-{safe_token}</segment>\n'
        "    </segments>\n"
        "  </file>\n"
        "</nzb>\n"
    )


def token_from_nzb(payload):
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    match = TOKEN_IN_NZB.search(text) or TOKEN_ANYWHERE.search(text)
    return match.group(1) if match else None


def _error(code, description, status=400):
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<error code="{int(code)}" description="{escape(description)}"/>\n'
    )
    return Response(body, status=status, mimetype="application/xml")


def _authorized():
    expected = current_app.config["INDEXER_API_KEY"]
    given = request.args.get("apikey") or request.headers.get("X-Api-Key")
    return api_key_matches(given, expected)


def _caps():
    body = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="svtplayarr" strapline="SVT Play" url="http://svtplayarr:2626/"/>
  <limits max="100" default="50"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q"/>
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2040" name="Movies/HD"/>
    </category>
    <category id="5000" name="TV">
      <subcat id="5040" name="TV/HD"/>
    </category>
  </categories>
</caps>
"""
    return Response(body, mimetype="application/xml")


def _grab_url(token):
    key = current_app.config["INDEXER_API_KEY"]
    return f"{request.host_url}newznab/api?t=get&id={token}&apikey={key}"


def _rss(items):
    rows = []
    now = formatdate(datetime.now(timezone.utc).timestamp(), usegmt=True)
    for item in items:
        url = escape(_grab_url(item["token"]))
        title = escape(item["title"])
        category = item["newznab_category"]
        attributes = [
            f'<newznab:attr name="category" value="{category}"/>',
            f'<newznab:attr name="size" value="{int(item["size"])}"/>',
            '<newznab:attr name="language" value="sv"/>',
        ]
        if item.get("season") is not None:
            attributes.append(f'<newznab:attr name="season" value="{int(item["season"])}"/>')
        if item.get("episode") is not None:
            attributes.append(f'<newznab:attr name="episode" value="{int(item["episode"])}"/>')
        rows.append(
            "    <item>\n"
            f"      <title>{title}</title>\n"
            f'      <guid isPermaLink="false">{escape(item["token"])}</guid>\n'
            f"      <link>{url}</link>\n"
            f"      <pubDate>{now}</pubDate>\n"
            f"      <category>{category}</category>\n"
            f'      <enclosure url="{url}" length="{int(item["size"])}" type="application/x-nzb"/>\n'
            f"      {''.join(attributes)}\n"
            "    </item>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">\n'
        "  <channel>\n"
        "    <title>svtplayarr</title>\n"
        f"{chr(10).join(rows)}\n"
        "  </channel>\n"
        "</rss>\n"
    )
    return Response(body, mimetype="application/xml")


def _collect(kind, query, season, episode, year):
    catalog = current_app.config["CATALOG"]
    hits = []
    if not query:
        if kind in ("tv", "both"):
            hits.extend(catalog.sample_tv())
        if kind in ("movie", "both"):
            hits.extend(catalog.sample_movies())
        return hits
    if kind in ("tv", "both"):
        hits.extend(catalog.search_tv(query, season, episode))
    if kind in ("movie", "both"):
        hits.extend(catalog.search_movies(query, year))
    return hits


def _release_from_hit(hit, query):
    if hit["kind"] == "tv":
        return {
            "title": tv_release_title(hit, query),
            "url": hit["url"],
            "category": "tv",
            "newznab_category": "5000",
            "season": hit["season"],
            "episode": hit["episode"],
            "size": RELEASE_SIZE,
        }
    return {
        "title": movie_release_title(hit),
        "url": hit["url"],
        "category": "movies",
        "newznab_category": "2000",
        "size": RELEASE_SIZE,
    }


@bp.route("/api", methods=["GET", "POST"])
def api():
    if not _authorized():
        return _error(100, "Incorrect API key", 401)
    mode = (request.values.get("t") or "search").lower()
    if mode == "caps":
        return _caps()
    if mode == "get":
        release = current_app.config["STORE"].get_release(request.values.get("id", ""))
        if not release:
            return _error(300, "No such item", 404)
        filename = f'{release["title"]}.svtplayarr-{release["token"]}.nzb'
        response = Response(
            build_nzb(release["token"], release["title"]),
            mimetype="application/x-nzb",
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    categories = parse_categories(request.values.get("cat"))
    kind = search_kind(mode, categories)
    query = (request.values.get("q") or "").strip()
    season = parse_int(request.values.get("season"))
    episode = parse_int(request.values.get("ep") or request.values.get("episode"))
    year = parse_int(request.values.get("year"))
    limit = parse_int(request.values.get("limit")) or 50
    limit = max(1, min(limit, 100))
    offset = parse_int(request.values.get("offset")) or 0
    try:
        hits = _collect(kind, query, season, episode, year)
    except Exception as exc:
        return _error(500, str(exc), 503)
    store = current_app.config["STORE"]
    items = []
    for hit in hits[offset:offset + limit]:
        release = _release_from_hit(hit, query)
        release["token"] = store.save_release(
            release["title"], release["url"], release["category"], release["size"]
        )
        items.append(release)
    return _rss(items)

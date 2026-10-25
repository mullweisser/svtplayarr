import re
import requests
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

SVT_GRAPHQL_URL = "https://contento.svt.se/graphql"
SVT_SEARCH_QUERY = """
query Search($query: String!, $maxHits: Int) {
  searchPage(query: $query, maxHits: $maxHits) {
    flat {
      hits {
        teaser {
          item {
            __typename
            name
            videoSvtId
            urls { svtplay }
          }
        }
      }
    }
  }
}
"""
SVT_PLAYABLE_PREFIXES = ("/video/", "/klipp/", "/kanaler/")

class ContentMatcher:
    def __init__(self):
        self.svt_patterns = [
            r'svtplay\.se',
            r'svt\.se/video',
            r'oppetarkiv\.se'
        ]
        self.nrk_patterns = [
            r'nrk\.no',
            r'tv\.nrk\.no'
        ]
    
    def extract_urls_from_justwatch(self, items: List[Dict], provider: str) -> List[str]:
        urls = []
        
        for item in items:
            # Extract URLs from JustWatch item
            offers = item.get('offers', [])
            for offer in offers:
                if provider.lower() in offer.get('provider_id', '').lower():
                    url = offer.get('urls', {}).get('standard_web', '')
                    if url:
                        urls.append(url)
        
        return urls
    
    def find_direct_urls(self, title: str, provider: str) -> List[str]:
        urls = []
        
        if provider == 'svt':
            urls.extend(self._search_svt_direct(title))
        elif provider == 'nrk':
            urls.extend(self._search_nrk_direct(title))
        
        return urls
    
    def _search_svt_direct(self, title: str) -> List[str]:
        try:
            # Search SVT Play directly
            search_url = f"https://www.svtplay.se/sok?q={title.replace(' ', '+')}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(search_url, headers=headers, timeout=10)
            if response.status_code == 200:
                # Parse HTML for video URLs
                urls = re.findall(r'href="(/video/[^"]+)"', response.text)
                return [f"https://www.svtplay.se{url}" for url in urls[:5]]
                
        except Exception as e:
            logger.error(f"SVT direct search error: {e}")
        
        return []
    
    def _search_nrk_direct(self, title: str) -> List[str]:
        try:
            # Search NRK directly
            search_url = f"https://tv.nrk.no/sok?q={title.replace(' ', '+')}"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(search_url, headers=headers, timeout=10)
            if response.status_code == 200:
                # Parse HTML for video URLs
                urls = re.findall(r'href="(/serie/[^"]+|/program/[^"]+)"', response.text)
                return [f"https://tv.nrk.no{url}" for url in urls[:5]]
                
        except Exception as e:
            logger.error(f"NRK direct search error: {e}")
        
        return []
    
    def validate_url(self, url: str, provider: str) -> bool:
        patterns = self.svt_patterns if provider == 'svt' else self.nrk_patterns
        
        for pattern in patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        
        return False

    def search_svt(self, title: str) -> List[Dict]:
        try:
            response = requests.post(
                SVT_GRAPHQL_URL,
                json={
                    "query": SVT_SEARCH_QUERY,
                    "variables": {"query": title, "maxHits": 10},
                },
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if response.status_code != 200:
                logger.error(f"SVT search HTTP {response.status_code}")
                return []
            payload = response.json()
        except Exception as e:
            logger.error(f"SVT search error: {e}")
            return []

        if payload.get("errors") and not payload.get("data"):
            logger.error(f"SVT search error: {payload['errors']}")
            return []

        hits = (
            ((payload.get("data") or {}).get("searchPage") or {}).get("flat") or {}
        ).get("hits") or []
        items = []
        for hit in hits:
            item = self.svt_result_from_item(((hit or {}).get("teaser") or {}).get("item"))
            if item:
                items.append(item)
        return self.rank_svt_results(items, title)

    def svt_result_from_item(self, item: Optional[Dict]) -> Optional[Dict]:
        if not item:
            return None
        name = (item.get("name") or "").strip()
        url = self.svt_play_url(item)
        if not name or not url:
            return None
        return {
            "title": name,
            "url": url,
            "kind": item.get("__typename") or "",
        }

    def svt_play_url(self, item: Dict) -> Optional[str]:
        path = ((item.get("urls") or {}).get("svtplay") or "").strip()
        video_id = (item.get("videoSvtId") or "").strip()
        # svtplay-dl only accepts /video, /klipp, and /kanaler pages.
        if path.startswith(SVT_PLAYABLE_PREFIXES):
            return f"https://www.svtplay.se{path}"
        if video_id:
            return f"https://www.svtplay.se/video/{video_id}"
        return None

    def rank_svt_results(self, items: List[Dict], title: str) -> List[Dict]:
        return sorted(
            items,
            key=lambda item: self.svt_title_score(title, item.get("title") or ""),
            reverse=True,
        )

    def best_svt_match(self, items: List[Dict], title: str) -> Optional[Dict]:
        ranked = self.rank_svt_results(items, title)
        return ranked[0] if ranked else None

    def svt_title_score(self, query: str, candidate: str) -> int:
        wanted = query.casefold().strip()
        found = candidate.casefold().strip()
        if not wanted or not found:
            return 0
        if wanted == found:
            return 1000
        if wanted in found or found in wanted:
            return 500 + int(100 * min(len(wanted), len(found)) / max(len(wanted), len(found)))
        wanted_words = set(wanted.split())
        found_words = set(found.split())
        return len(wanted_words & found_words)

    def get_best_match_url(self, urls: List[str], title: str) -> Optional[str]:
        if not urls:
            return None
        
        # Simple scoring based on URL content
        scored_urls = []
        title_words = title.lower().split()
        
        for url in urls:
            score = 0
            url_lower = url.lower()
            
            for word in title_words:
                if word in url_lower:
                    score += 1
            
            scored_urls.append((score, url))
        
        # Sort by score and return best match
        scored_urls.sort(key=lambda x: x[0], reverse=True)
        return scored_urls[0][1] if scored_urls else urls[0]

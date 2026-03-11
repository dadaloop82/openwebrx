from . import Controller
from owrx.bookmarks import Bookmarks
from owrx.sdr import SdrService
import json
import logging

logger = logging.getLogger(__name__)


class AllBookmarksApiController(Controller):
    """API endpoint that returns ALL bookmarks with matching profile info."""

    def indexAction(self):
        try:
            # Get all bookmarks (no frequency range filter)
            all_bookmarks = Bookmarks.getSharedInstance().getBookmarks()

            # Build profile frequency ranges
            profiles = []
            try:
                for source_id, source in SdrService.getActiveSources().items():
                    source_name = source.getName()
                    source_profiles = source.getProfiles()
                    if hasattr(source_profiles, 'items'):
                        for p_id, profile in source_profiles.items():
                            try:
                                cf = profile["center_freq"]
                                sr = profile["samp_rate"]
                            except (KeyError, TypeError):
                                continue
                            if cf and sr:
                                try:
                                    pname = profile["name"]
                                except (KeyError, TypeError):
                                    pname = str(p_id)
                                profiles.append({
                                    "id": "{}|{}".format(source_id, p_id),
                                    "name": "{} {}".format(source_name, pname),
                                    "center_freq": cf,
                                    "samp_rate": sr,
                                    "low": cf - sr // 2,
                                    "high": cf + sr // 2,
                                })
            except Exception as e:
                logger.exception("Error building profile list for bookmarks API: %s", e)

            # Build bookmark list with best matching profile
            result = []
            for b in all_bookmarks:
                freq = b.getFrequency()
                # Find best profile: smallest range that contains this frequency
                best_profile = None
                best_range = float('inf')
                for p in profiles:
                    if p["low"] <= freq <= p["high"]:
                        r = p["high"] - p["low"]
                        if r < best_range:
                            best_range = r
                            best_profile = p
                entry = {
                    "name": b.getName(),
                    "frequency": freq,
                    "modulation": b.getModulation(),
                    "underlying": b.getUnderlying(),
                    "description": b.getDescription(),
                }
                if best_profile:
                    entry["profile_id"] = best_profile["id"]
                result.append(entry)

            data = json.dumps(result)
            self.send_response(data, content_type="application/json")
        except Exception as e:
            logger.exception("Error in AllBookmarksApiController: %s", e)
            self.send_response(
                json.dumps({"error": str(e)}),
                code=500,
                content_type="application/json"
            )

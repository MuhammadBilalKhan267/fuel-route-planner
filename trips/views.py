import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .errors import PlanningError
from .provider import CallCounts, HeiGitClient
from .service import plan_trip
from .stations import dataset_status
from .validation import parse_request

logger = logging.getLogger(__name__)


@require_GET
def map_page(request):
    return render(
        request,
        "trips/map.html",
        {
            "tiles": {"url": settings.MAP_TILE_URL, "attribution": settings.MAP_TILE_ATTRIBUTION},
            "defaults": {
                "initial_fuel_gallons": settings.DEFAULT_INITIAL_FUEL_GALLONS,
                "initial_fuel_price_usd_per_gallon": settings.DEFAULT_INITIAL_FUEL_PRICE_USD,
                "verify_stops": settings.DEFAULT_VERIFY_STOPS,
                "station_corridor_miles": settings.DEFAULT_STATION_CORRIDOR_MILES,
            },
        },
    )


@require_GET
def health(request):
    return JsonResponse(
        {"provider_configured": bool(settings.ORS_API_KEY), "stations": dataset_status()}
    )


def reject_constant(value):
    raise ValueError(f"Nonstandard JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


# A stateless, anonymous JSON query endpoint. It has no session/account writes.
@csrf_exempt
def plan_route(request):
    counts = CallCounts()
    if request.method != "POST":
        response = JsonResponse(
            {"error": {"code": "method_not_allowed", "message": "Use POST with JSON."}}, status=405
        )
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return JsonResponse(
            {"error": {"code": "unsupported_media_type", "message": "Use application/json."}},
            status=415,
        )
    try:
        try:
            payload = json.loads(
                request.body.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            )
        except RequestDataTooBig as exc:
            raise PlanningError("request_too_large", "Request body exceeds 16 KiB.", 413) from exc
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise PlanningError(
                "invalid_json", "Body must be valid JSON with finite numbers.", 400
            ) from exc
        parsed = parse_request(payload)
        provider = HeiGitClient(counts)
        try:
            result = plan_trip(parsed, provider)
        finally:
            provider.close()
        return JsonResponse(result)
    except PlanningError as exc:
        return JsonResponse(
            {
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "metadata": counts.as_dict(),
            },
            status=exc.status,
        )
    except Exception:
        # Do not log provider credentials, request bodies or upstream URLs.
        logger.exception("Unexpected planning failure")
        return JsonResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected planning error occurred.",
                },
                "metadata": counts.as_dict(),
            },
            status=500,
        )

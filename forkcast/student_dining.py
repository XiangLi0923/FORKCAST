"""Snapshot-pinned student catalog projection and signed pagination."""

import base64
import binascii
import json
from hmac import compare_digest, digest

from .decision_models import Evidence
from .errors import Fault
from .models import Catalog, Fact, SupplySnapshot


def encode_cursor(key: bytes, fields: tuple) -> str:
    payload = json.dumps(fields, separators=(",", ":")).encode()
    mac = digest(key, b"dining:" + payload, "sha256")
    return base64.urlsafe_b64encode(mac + payload).decode().rstrip("=")


def decode_cursor(key: bytes, cursor: str) -> tuple:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        mac, payload = raw[:32], raw[32:]
        if not compare_digest(mac, digest(key, b"dining:" + payload, "sha256")):
            raise ValueError("signature")
        run_id, snapshot_id, venue_id, limit, last = json.loads(payload)
        return run_id, snapshot_id, venue_id, limit, last
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise Fault("INVALID_INPUT", "Invalid dining options cursor.") from exc


def _fresh(fact, now):
    return fact.value if fact is not None and fact.freshness(now, dynamic=True) == "FRESH" else None


def _evidence(dish_id, field, fact, now, *, dynamic=True):
    state = fact.freshness(now, dynamic=dynamic) if fact else "UNKNOWN"
    code = ("STALE_FACT" if state == "STALE" else "PRICE_UNKNOWN"
            if field == "price_cents" and state == "UNKNOWN" else "SUPPLY_UNVERIFIED"
            if state == "UNKNOWN" else "SIMULATION_DATA")
    return Evidence(
        code=code, entity_id=dish_id, field=field, source_kind=fact.source_kind if fact else "UNKNOWN",
        source_ref=fact.source_ref if fact else None, as_of=fact.as_of if fact else None, freshness=state,
        value=fact.value if state == "FRESH" else None,
    ).model_dump(mode="json")


def project_dining(snapshot: SupplySnapshot, catalog: Catalog, *, venue_id: str | None,
                   after: str | None, limit: int):
    now = snapshot.ref.clock_at
    venue_states = {row.venue_id: row for row in snapshot.venue_states}
    dish_states = {row.dish_id: row for row in snapshot.dish_states}
    names = {dish.dish_id: dish.name for dish in catalog.dishes}
    venues = []
    unknown = Fact(value=None, source_kind="UNKNOWN", source_ref=None, as_of=None, valid_until=None,
                   confidence="UNKNOWN")
    for venue in sorted(catalog.venues, key=lambda row: row.venue_id):
        if venue_id is None or venue_id == venue.venue_id:
            current = venue_states.get(venue.venue_id)
            venues.append({
                "venue_id": venue.venue_id, "name": venue.name.model_dump(mode="json"),
                "location_label": venue.location_label.model_dump(mode="json"),
                "opening_hours": venue.opening_hours.model_dump(mode="json"),
                "service_status": (current.service_status if current else unknown).model_dump(mode="json"),
                "queue_min": (current.queue_min if current else unknown).model_dump(mode="json"),
            })

    bundles = [row for row in sorted(catalog.bundles, key=lambda row: row.bundle_id)
               if (venue_id is None or row.venue_id == venue_id) and (after is None or row.bundle_id > after)]
    options = []
    for bundle in bundles[:limit]:
        prices, counts, statuses, evidence = [], [], [], []
        for item in bundle.items:
            state = dish_states.get(item.dish_id)
            status, stock, price = (
                (state.service_status, state.stock_portions, state.price_cents) if state else (None, None, None)
            )
            status_value, stock_value, price_value = (_fresh(fact, now) for fact in (status, stock, price))
            statuses.append(status_value)
            prices.append(price_value * item.portions if price_value is not None else None)
            counts.append(stock_value // item.portions if stock_value is not None else None)
            evidence.extend(_evidence(item.dish_id, field, fact, now) for field, fact in (
                ("service_status", status), ("stock_portions", stock), ("price_cents", price),
            ))
            evidence.append(_evidence(item.dish_id, "name", names[item.dish_id], now, dynamic=False))
        if snapshot.active_window_id is None or "UNAVAILABLE" in statuses:
            supply = "UNAVAILABLE"
        elif "SOLD_OUT" in statuses or 0 in counts:
            supply = "SOLD_OUT"
        elif all(status == "AVAILABLE" for status in statuses) and None not in counts:
            supply = "AVAILABLE"
        else:
            supply = "UNKNOWN"
        options.append({
            "bundle_id": bundle.bundle_id, "venue_id": bundle.venue_id, "label": bundle.label,
            "anchor_dish_id": bundle.anchor_dish_id, "items": [item.model_dump(mode="json") for item in bundle.items],
            "price_cents": sum(prices) if None not in prices else None,
            "available_bundle_portions": min(counts) if None not in counts else None,
            "supply_state": supply, "evidence": evidence,
        })
    return venues, options, bundles[limit - 1].bundle_id if len(bundles) > limit else None

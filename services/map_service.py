import difflib
import json
import math
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import folium
import pandas as pd
import pydeck as pdk
import streamlit as st
from folium.features import DivIcon
from streamlit_folium import st_folium

from services.config_service import add_chinese_tiles, day_color, rgb_to_hex


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def render_day_legend(days: list[int]):
    if not days:
        return
    items = []
    for day_no in days:
        color_hex = rgb_to_hex(day_color(day_no))
        items.append(
            f"<span style='display:inline-flex;align-items:center;margin-right:12px;'>"
            f"<span style='width:10px;height:10px;border-radius:50%;background:{color_hex};display:inline-block;margin-right:6px;'></span>"
            f"Day {day_no}</span>"
        )
    st.markdown("".join(items), unsafe_allow_html=True)


def geocode_with_amap(amap_api_key: str, address: str):
    if not amap_api_key:
        return False, "未读取到 AMAP_API_KEY"
    if not address.strip():
        return False, "地址为空"

    base_url = "https://restapi.amap.com/v3/geocode/geo"
    query = urlencode({"key": amap_api_key, "address": address})
    url = f"{base_url}?{query}"

    try:
        with urlopen(url, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if str(payload.get("status")) != "1":
            return False, f"高德接口失败: {payload.get('info', 'unknown')}"

        geocodes = payload.get("geocodes", [])
        if not geocodes:
            return False, "高德未返回地理编码结果"

        first = geocodes[0]
        location = first.get("location", "")
        if "," not in location:
            return False, "高德返回坐标格式异常"

        lon_str, lat_str = location.split(",", 1)
        return True, {
            "lon": float(lon_str),
            "lat": float(lat_str),
            "formatted_address": first.get("formatted_address", address),
            "province": first.get("province", ""),
            "city": first.get("city", ""),
            "district": first.get("district", ""),
            "adcode": first.get("adcode", ""),
            "level": first.get("level", ""),
        }
    except Exception as e:
        return False, f"高德调用异常: {e}"


@st.cache_data(ttl=86400, show_spinner=False)
def geocode_with_amap_cached(amap_api_key: str, address: str):
    return geocode_with_amap(amap_api_key, address)


@st.cache_data(ttl=86400, show_spinner=False)
def get_destination_center(amap_api_key: str, destination: str):
    return geocode_with_amap(amap_api_key, destination)


def parse_amap_polyline(polyline_text: str) -> list:
    points = []
    if not polyline_text:
        return points
    for pair in polyline_text.split(";"):
        if "," not in pair:
            continue
        lon_str, lat_str = pair.split(",", 1)
        try:
            points.append([float(lat_str), float(lon_str)])
        except Exception:
            continue
    return points


def get_amap_route(amap_api_key: str, origin_lon: float, origin_lat: float, dest_lon: float, dest_lat: float, mode: str = "driving"):
    if not amap_api_key:
        return False, "未读取到 AMAP_API_KEY"

    mode = mode if mode in {"driving", "walking"} else "driving"
    base_url = "https://restapi.amap.com/v3/direction/walking" if mode == "walking" else "https://restapi.amap.com/v3/direction/driving"

    query = urlencode(
        {
            "key": amap_api_key,
            "origin": f"{origin_lon},{origin_lat}",
            "destination": f"{dest_lon},{dest_lat}",
            "extensions": "all",
        }
    )
    url = f"{base_url}?{query}"

    try:
        with urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if str(payload.get("status")) != "1":
            return False, f"路线接口失败: {payload.get('info', 'unknown')}"

        route = payload.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            return False, "路线接口未返回路径"

        first = paths[0]
        distance_m = float(first.get("distance", 0) or 0)
        duration_s = float(first.get("duration", 0) or 0)

        polyline_points = []
        for step in first.get("steps", []):
            polyline_points.extend(parse_amap_polyline(step.get("polyline", "")))

        if not polyline_points:
            polyline_points = [[origin_lat, origin_lon], [dest_lat, dest_lon]]

        return True, {
            "distance_km": distance_m / 1000.0,
            "duration_min": duration_s / 60.0,
            "polyline": polyline_points,
        }
    except Exception as e:
        return False, f"路线调用异常: {e}"


@st.cache_data(ttl=21600, show_spinner=False)
def get_amap_route_cached(amap_api_key: str, origin_lon: float, origin_lat: float, dest_lon: float, dest_lat: float, mode: str = "driving"):
    return get_amap_route(amap_api_key, origin_lon, origin_lat, dest_lon, dest_lat, mode=mode)


def search_nearby_pois_with_amap(amap_api_key: str, lon: float, lat: float, radius: int = 5000, max_points: int = 8) -> list:
    if not amap_api_key:
        return []

    base_url = "https://restapi.amap.com/v3/place/around"
    query = urlencode(
        {
            "key": amap_api_key,
            "location": f"{lon},{lat}",
            "radius": radius,
            "sortrule": "distance",
            "offset": max_points,
            "page": 1,
            "extensions": "base",
            "keywords": "景点 博物馆 公园 步行街",
        }
    )
    url = f"{base_url}?{query}"

    try:
        with urlopen(url, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if str(payload.get("status")) != "1":
            return []

        pois = payload.get("pois", [])
        results = []
        for p in pois:
            location = p.get("location", "")
            if "," not in location:
                continue
            lon_str, lat_str = location.split(",", 1)
            try:
                results.append({"name": p.get("name", "附近景点"), "lon": float(lon_str), "lat": float(lat_str)})
            except Exception:
                continue
        return results
    except Exception:
        return []


@st.cache_data(ttl=86400, show_spinner=False)
def search_nearby_pois_with_amap_cached(amap_api_key: str, lon: float, lat: float, radius: int = 5000, max_points: int = 8) -> list:
    return search_nearby_pois_with_amap(amap_api_key, lon, lat, radius=radius, max_points=max_points)


def normalize_spot_name(spot_name: str) -> str:
    name = str(spot_name).strip()
    name = re.sub(r"[（(].*?[）)]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -:：")
    return name


def _spot_name_variants(spot_name: str) -> list:
    name = normalize_spot_name(spot_name)
    if not name:
        return []
    variants = [name]
    for suffix in ["景区", "景点", "旅游区", "公园", "博物馆", "古镇", "广场", "老街"]:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            variants.append(name[: -len(suffix)])

    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def resolve_spot_point(
    amap_api_key: str,
    destination: str,
    spot_name: str,
    day_no,
    order_no,
    search_radius_m: int = 15000,
    destination_radius_km: float = 120.0,
):
    normalized = normalize_spot_name(spot_name)
    if not normalized:
        return None

    ok_dest, dest_geo = get_destination_center(amap_api_key, destination)
    if not ok_dest:
        return None
    dest_lon = float(dest_geo["lon"])
    dest_lat = float(dest_geo["lat"])

    queries = []
    for v in _spot_name_variants(normalized):
        queries.extend([f"{destination} {v}", f"{destination}{v}", v])

    seen_q = set()
    for q in queries:
        q = q.strip()
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        ok, geo = geocode_with_amap_cached(amap_api_key, q)
        if ok:
            dist_to_dest = haversine_km(dest_lat, dest_lon, float(geo["lat"]), float(geo["lon"]))
            if dist_to_dest > destination_radius_km:
                continue
            return {
                "day": day_no,
                "order": order_no,
                "name": normalized,
                "lat": geo["lat"],
                "lon": geo["lon"],
                "source": "geocode",
            }

    nearby = search_nearby_pois_with_amap_cached(amap_api_key, dest_lon, dest_lat, radius=search_radius_m, max_points=30)
    if not nearby:
        return None

    candidates = []
    target_variants = _spot_name_variants(normalized)
    for poi in nearby:
        poi_name = normalize_spot_name(poi.get("name", ""))
        if not poi_name:
            continue
        best_score = 0.0
        for v in target_variants:
            score = difflib.SequenceMatcher(None, v, poi_name).ratio()
            best_score = max(best_score, score)
        candidates.append((best_score, poi_name, poi))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_score, _, top_poi = candidates[0]
    if top_score < 0.42:
        return None

    return {
        "day": day_no,
        "order": order_no,
        "name": top_poi.get("name", normalized),
        "lat": float(top_poi["lat"]),
        "lon": float(top_poi["lon"]),
        "source": "fuzzy_nearby",
    }


def compact_route_points(amap_api_key: str, route_points: list, max_segment_km: float = 80.0) -> list:
    if len(route_points) <= 1:
        return route_points

    compacted = [route_points[0]]
    seen_xy = {(round(float(route_points[0]["lat"]), 5), round(float(route_points[0]["lon"]), 5))}

    for current in route_points[1:]:
        prev = compacted[-1]
        dist = haversine_km(prev["lat"], prev["lon"], current["lat"], current["lon"])
        if dist <= max_segment_km:
            compacted.append(current)
            seen_xy.add((round(float(current["lat"]), 5), round(float(current["lon"]), 5)))
            continue

        nearby = search_nearby_pois_with_amap_cached(
            amap_api_key,
            prev["lon"],
            prev["lat"],
            radius=max(2000, int(max_segment_km * 1000)),
            max_points=12,
        )
        replacement = None
        for poi in nearby:
            key = (round(float(poi["lat"]), 5), round(float(poi["lon"]), 5))
            if key in seen_xy:
                continue
            replacement = {
                "day": current.get("day", prev.get("day", "?")),
                "order": current.get("order", len(compacted) + 1),
                "name": poi.get("name", current.get("name", "附近景点")),
                "lat": poi["lat"],
                "lon": poi["lon"],
                "source": "compact_nearby",
            }
            break

        if replacement:
            compacted.append(replacement)
            seen_xy.add((round(float(replacement["lat"]), 5), round(float(replacement["lon"]), 5)))

    for i, p in enumerate(compacted, start=1):
        p["order"] = i
    return compacted


def build_day_route_points(
    amap_api_key: str,
    destination: str,
    day_item: dict,
    max_points: int = 5,
    max_segment_km: float = 80.0,
    destination_radius_km: float = 120.0,
) -> list:
    points = []
    highlights = day_item.get("highlights", [])
    if not isinstance(highlights, list):
        return points

    day_no = day_item.get("day", "?")
    for idx, spot in enumerate(highlights[:max_points], start=1):
        point = resolve_spot_point(
            amap_api_key,
            destination,
            str(spot),
            day_no,
            idx,
            destination_radius_km=destination_radius_km,
        )
        if point:
            points.append(point)

    dedup = []
    seen_xy = set()
    for p in points:
        key = (round(float(p["lat"]), 5), round(float(p["lon"]), 5))
        if key in seen_xy:
            continue
        seen_xy.add(key)
        dedup.append(p)

    if len(dedup) < 3:
        ok_dest, dest_geo = geocode_with_amap_cached(amap_api_key, destination)
        if ok_dest:
            nearby = search_nearby_pois_with_amap_cached(amap_api_key, dest_geo["lon"], dest_geo["lat"], radius=6000, max_points=10)
            order_start = len(dedup) + 1
            for poi in nearby:
                key = (round(float(poi["lat"]), 5), round(float(poi["lon"]), 5))
                if key in seen_xy:
                    continue
                seen_xy.add(key)
                dedup.append(
                    {
                        "day": day_no,
                        "order": order_start,
                        "name": poi["name"],
                        "lat": poi["lat"],
                        "lon": poi["lon"],
                        "source": "nearby",
                    }
                )
                order_start += 1
                if len(dedup) >= 3:
                    break

    if len(dedup) < 2 and isinstance(highlights, list) and len(highlights) >= 2:
        ok_dest, dest_geo = geocode_with_amap_cached(amap_api_key, destination)
        if ok_dest:
            base_lat = float(dest_geo["lat"])
            base_lon = float(dest_geo["lon"])
            synthetic = []
            for i, spot in enumerate(highlights[:2], start=1):
                synthetic.append(
                    {
                        "day": day_no,
                        "order": i,
                        "name": normalize_spot_name(spot) or f"景点{i}",
                        "lat": base_lat + (i - 1) * 0.008,
                        "lon": base_lon + (i - 1) * 0.008,
                        "source": "synthetic",
                    }
                )
            dedup = synthetic

    for i, p in enumerate(dedup, start=1):
        p["order"] = i

    dedup = compact_route_points(amap_api_key, dedup, max_segment_km=max_segment_km)
    return dedup[:max_points]


def render_day_route_map(
    amap_api_key: str,
    destination: str,
    day_item: dict,
    point_radius_px: int = 5,
    map_engine: str = "folium",
    max_segment_km: float = 80.0,
    route_mode: str = "driving",
    destination_radius_km: float = 120.0,
):
    route_points = build_day_route_points(
        amap_api_key,
        destination,
        day_item,
        max_segment_km=max_segment_km,
        destination_radius_km=destination_radius_km,
    )
    if len(route_points) < 2:
        st.caption("该日可定位景点不足 2 个，暂不绘制路线图。")
        return

    sources = {p.get("source", "") for p in route_points}
    if "synthetic" in sources:
        st.caption("该日部分点位使用目的地附近近似坐标，仅用于辅助连线展示。")
    if "fuzzy_nearby" in sources:
        st.caption("该日部分点位通过附近 POI 模糊匹配定位。")
    if "compact_nearby" in sources:
        st.caption("该日存在超远跳点，已自动替换为邻近景点以压缩路线距离。")

    points_df = pd.DataFrame(route_points)
    points_df["display_name"] = points_df.apply(lambda r: f"{int(r['order'])}. {r['name']}", axis=1)

    segment_rows = []
    label_rows = []
    total_km = 0.0
    route_polylines = []

    for i in range(len(route_points) - 1):
        a = route_points[i]
        b = route_points[i + 1]
        ok_route, route_info = get_amap_route_cached(amap_api_key, a["lon"], a["lat"], b["lon"], b["lat"], mode=route_mode)
        if ok_route:
            dist = float(route_info["distance_km"])
            duration_min = float(route_info["duration_min"])
            route_polylines.append(route_info["polyline"])
        else:
            dist = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            duration_min = max(1.0, dist / 35.0 * 60.0)
            route_polylines.append([[a["lat"], a["lon"]], [b["lat"], b["lon"]]])

        total_km += dist
        segment_rows.append({"路段": f"{a['name']} → {b['name']}", "距离(km)": round(dist, 2), "预计时长(分钟)": int(round(duration_min))})
        label_rows.append({"label": f"{dist:.1f} km", "lat": (a["lat"] + b["lat"]) / 2, "lon": (a["lon"] + b["lon"]) / 2})

    path_df = pd.DataFrame(
        [{"name": f"Day {day_item.get('day', '?')} Route", "path": [[pt[1], pt[0]] for seg in route_polylines for pt in seg]}]
    )
    label_df = pd.DataFrame(label_rows)

    center_lat = float(points_df["lat"].mean())
    center_lon = float(points_df["lon"].mean())

    if map_engine == "folium":
        m = folium.Map(location=[center_lat, center_lon], zoom_start=12, tiles=None, control_scale=True)
        add_chinese_tiles(m)

        for seg in route_polylines:
            folium.PolyLine(seg, color="#1e88e5", weight=5, opacity=0.85).add_to(m)

        for p in route_points:
            folium.CircleMarker(
                location=[p["lat"], p["lon"]],
                radius=max(3, point_radius_px),
                color="#ff5722",
                fill=True,
                fill_color="#ff5722",
                fill_opacity=0.95,
                popup=f"Day {p.get('day', '?')} 第{p['order']}站: {p['name']}",
                tooltip=f"{p['order']}. {p['name']}",
            ).add_to(m)

            folium.Marker(
                location=[p["lat"], p["lon"]],
                icon=DivIcon(
                    icon_size=(220, 24),
                    icon_anchor=(0, 0),
                    html=(
                        "<div style='font-size:12px;font-weight:600;color:#1f2937;"
                        "text-shadow:1px 1px 2px rgba(255,255,255,0.9);white-space:nowrap;'>"
                        f"{p['order']}. {p['name']}"
                        "</div>"
                    ),
                ),
            ).add_to(m)

        for row in label_rows:
            folium.Marker(
                location=[row["lat"], row["lon"]],
                icon=DivIcon(
                    icon_size=(80, 18),
                    icon_anchor=(0, 0),
                    html=(
                        "<div style='font-size:11px;color:#0d47a1;background:rgba(255,255,255,0.88);"
                        "padding:1px 4px;border-radius:4px;white-space:nowrap;'>"
                        f"{row['label']}"
                        "</div>"
                    ),
                ),
            ).add_to(m)

        st_folium(m, use_container_width=True, height=430, returned_objects=[], key=f"day_map_{day_item.get('day', '?')}")
        st.dataframe(points_df[["order", "name", "lat", "lon"]], hide_index=True, use_container_width=True)
        if segment_rows:
            total_min = sum(float(r.get("预计时长(分钟)", 0) or 0) for r in segment_rows)
            st.caption(f"当日总里程（真实路径）：{total_km:.2f} km | 预计总时长：{int(round(total_min))} 分钟")
            st.dataframe(pd.DataFrame(segment_rows), hide_index=True, use_container_width=True)
        return

    deck = pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=12, pitch=35),
        layers=[
            pdk.Layer("PathLayer", data=path_df, get_path="path", get_width=10, width_min_pixels=3, get_color=[33, 150, 243, 180]),
            pdk.Layer(
                "ScatterplotLayer",
                data=points_df,
                get_position="[lon, lat]",
                get_radius=110,
                radius_min_pixels=point_radius_px,
                get_fill_color=[255, 87, 34, 220],
                pickable=True,
            ),
            pdk.Layer(
                "TextLayer",
                data=points_df,
                get_position="[lon, lat]",
                get_text="display_name",
                get_size=16,
                sizeUnits="pixels",
                getPixelOffset=[0, -14],
                get_color=[255, 255, 255, 245],
                getAlignmentBaseline="bottom",
            ),
            pdk.Layer(
                "TextLayer",
                data=points_df,
                get_position="[lon, lat]",
                get_text="display_name",
                get_size=14,
                sizeUnits="pixels",
                getPixelOffset=[0, -14],
                get_color=[28, 28, 28, 240],
                getAlignmentBaseline="bottom",
            ),
            pdk.Layer(
                "TextLayer",
                data=label_df,
                get_position="[lon, lat]",
                get_text="label",
                get_size=14,
                sizeUnits="pixels",
                get_color=[255, 255, 255, 245],
                getAlignmentBaseline="center",
            ),
            pdk.Layer(
                "TextLayer",
                data=label_df,
                get_position="[lon, lat]",
                get_text="label",
                get_size=12,
                sizeUnits="pixels",
                get_color=[21, 101, 192, 235],
                getAlignmentBaseline="center",
            ),
        ],
        tooltip={"text": "Day {day} 第{order}站: {name}"},
    )

    st.pydeck_chart(deck, use_container_width=True)
    st.dataframe(points_df[["order", "name", "lat", "lon"]], hide_index=True, use_container_width=True)
    if segment_rows:
        total_min = sum(float(r.get("预计时长(分钟)", 0) or 0) for r in segment_rows)
        st.caption(f"当日总里程（真实路径）：{total_km:.2f} km | 预计总时长：{int(round(total_min))} 分钟")
        st.dataframe(pd.DataFrame(segment_rows), hide_index=True, use_container_width=True)


def render_multiday_route_map(
    amap_api_key: str,
    destination: str,
    structured_data: dict,
    point_radius_px: int = 5,
    map_engine: str = "folium",
    selected_days: list[int] | None = None,
    max_segment_km: float = 80.0,
    route_mode: str = "driving",
    destination_radius_km: float = 120.0,
):
    daily_plan = structured_data.get("daily_plan", [])
    if not isinstance(daily_plan, list) or not daily_plan:
        return

    all_points = []
    all_paths = []
    summary_rows = []

    for day_item in daily_plan:
        day_no = day_item.get("day", 1)
        if selected_days and day_no not in selected_days:
            continue

        route_points = build_day_route_points(
            amap_api_key,
            destination,
            day_item,
            max_points=5,
            max_segment_km=max_segment_km,
            destination_radius_km=destination_radius_km,
        )
        if not route_points:
            continue

        color = day_color(day_no)
        for p in route_points:
            all_points.append(
                {
                    "day": day_no,
                    "order": p["order"],
                    "name": p["name"],
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "source": p.get("source", ""),
                    "color": color,
                }
            )

        day_duration_min = 0.0
        if len(route_points) >= 2:
            day_polyline = []
            day_dist_km = 0.0
            for i in range(len(route_points) - 1):
                a = route_points[i]
                b = route_points[i + 1]
                ok_route, route_info = get_amap_route_cached(amap_api_key, a["lon"], a["lat"], b["lon"], b["lat"], mode=route_mode)
                if ok_route:
                    day_dist_km += float(route_info["distance_km"])
                    day_duration_min += float(route_info["duration_min"])
                    day_polyline.extend([[pt[1], pt[0]] for pt in route_info["polyline"]])
                else:
                    fallback_km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
                    day_dist_km += fallback_km
                    day_duration_min += max(1.0, fallback_km / 35.0 * 60.0)
                    day_polyline.extend([[a["lon"], a["lat"]], [b["lon"], b["lat"]]])

            all_paths.append({"day": day_no, "path": day_polyline, "color": color})
        else:
            day_dist_km = 0.0

        summary_rows.append(
            {
                "天数": f"Day {day_no}",
                "景点数": len(route_points),
                "路线里程(km)": round(day_dist_km, 2),
                "预计时长(分钟)": int(round(day_duration_min)),
            }
        )

    if len(all_points) < 2:
        st.caption("可绘制的多日点位不足，暂不显示总览路线图。")
        return

    points_df = pd.DataFrame(all_points)
    paths_df = pd.DataFrame(all_paths) if all_paths else pd.DataFrame(columns=["day", "path", "color"])

    center_lat = float(points_df["lat"].mean())
    center_lon = float(points_df["lon"].mean())

    if map_engine == "folium":
        m = folium.Map(location=[center_lat, center_lon], zoom_start=11, tiles=None, control_scale=True)
        add_chinese_tiles(m)

        for row in all_paths:
            color_hex = rgb_to_hex(row.get("color"))
            latlon_path = [[pt[1], pt[0]] for pt in row.get("path", [])]
            if len(latlon_path) >= 2:
                folium.PolyLine(latlon_path, color=color_hex, weight=5, opacity=0.9, popup=f"Day {row.get('day', '?')}").add_to(m)

        for p in all_points:
            color_hex = rgb_to_hex(p.get("color"))
            folium.CircleMarker(
                location=[p["lat"], p["lon"]],
                radius=max(3, point_radius_px),
                color=color_hex,
                fill=True,
                fill_color=color_hex,
                fill_opacity=0.95,
                popup=f"Day {p['day']} 第{p['order']}站: {p['name']}",
                tooltip=f"Day {p['day']} 第{p['order']}站: {p['name']}",
            ).add_to(m)

        legend_days = sorted({int(p["day"]) for p in all_points if isinstance(p.get("day"), int)})
        render_day_legend(legend_days)
        st_folium(m, use_container_width=True, height=480, returned_objects=[], key="multiday_map")

        if summary_rows:
            st.caption("按天分段统计（颜色对应地图线路）")
            st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
        return

    layers = []
    if not paths_df.empty:
        layers.append(
            pdk.Layer("PathLayer", data=paths_df, get_path="path", get_color="color", get_width=12, width_min_pixels=4, pickable=True)
        )

    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            data=points_df,
            get_position="[lon, lat]",
            get_fill_color="color",
            get_radius=120,
            radius_min_pixels=point_radius_px,
            pickable=True,
        )
    )

    deck = pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=11, pitch=30),
        layers=layers,
        tooltip={"text": "Day {day} 第{order}站: {name}"},
    )

    st.pydeck_chart(deck, use_container_width=True)

    if summary_rows:
        st.caption("按天分段统计（颜色对应地图线路）")
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)


def _downsample_points(points: list[tuple[float, float]], max_points: int = 80) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def build_day_route_snapshot_url(
    amap_api_key: str,
    destination: str,
    day_item: dict,
    max_segment_km: float = 80.0,
    route_mode: str = "driving",
    destination_radius_km: float = 120.0,
    size: str = "900*450",
) -> str | None:
    route_points = build_day_route_points(
        amap_api_key=amap_api_key,
        destination=destination,
        day_item=day_item,
        max_points=5,
        max_segment_km=max_segment_km,
        destination_radius_km=destination_radius_km,
    )
    if len(route_points) < 2:
        return None

    polyline_lnglat: list[tuple[float, float]] = []
    for i in range(len(route_points) - 1):
        a = route_points[i]
        b = route_points[i + 1]
        ok_route, route_info = get_amap_route_cached(
            amap_api_key,
            a["lon"],
            a["lat"],
            b["lon"],
            b["lat"],
            mode=route_mode,
        )
        if ok_route:
            for lat, lon in route_info.get("polyline", []):
                point = (float(lon), float(lat))
                if not polyline_lnglat or polyline_lnglat[-1] != point:
                    polyline_lnglat.append(point)
        else:
            fallback = [(float(a["lon"]), float(a["lat"])), (float(b["lon"]), float(b["lat"]))]
            for point in fallback:
                if not polyline_lnglat or polyline_lnglat[-1] != point:
                    polyline_lnglat.append(point)

    if len(polyline_lnglat) < 2:
        return None

    polyline_lnglat = _downsample_points(polyline_lnglat, max_points=80)
    points_text = ";".join([f"{lon:.6f},{lat:.6f}" for lon, lat in polyline_lnglat])

    day_no = day_item.get("day", "?")
    markers = []
    for p in route_points:
        order = p.get("order", "")
        lon = float(p["lon"])
        lat = float(p["lat"])
        markers.append(f"mid,0xFF5722,{order}:{lon:.6f},{lat:.6f}")

    center_lon = sum(float(p["lon"]) for p in route_points) / len(route_points)
    center_lat = sum(float(p["lat"]) for p in route_points) / len(route_points)

    query = {
        "key": amap_api_key,
        "size": size,
        "zoom": "12",
        "location": f"{center_lon:.6f},{center_lat:.6f}",
        "traffic": "0",
        "path": f"8,0x1E88E5,1,,:{points_text}",
        "markers": "|".join(markers),
    }
    base_url = "https://restapi.amap.com/v3/staticmap"
    return f"{base_url}?{urlencode(query)}"


def repair_unlocatable_daily_highlights(
    amap_api_key: str,
    destination: str,
    structured_data: dict,
    max_segment_km: float = 80.0,
    destination_radius_km: float = 120.0,
    max_days_to_fix: int = 5,
    time_budget_s: float = 12.0,
) -> dict:
    daily_plan = structured_data.get("daily_plan", [])
    if not isinstance(daily_plan, list) or not daily_plan:
        return {"updated": False, "days_updated": 0, "spots_replaced": 0}

    days_updated = 0
    spots_replaced = 0

    started_at = time.time()
    processed_days = 0

    for day_item in daily_plan:
        if processed_days >= max_days_to_fix:
            break
        if time.time() - started_at > time_budget_s:
            break

        original = day_item.get("highlights", [])
        if not isinstance(original, list) or not original:
            continue
        processed_days += 1

        original_clean = [normalize_spot_name(x) for x in original if str(x).strip()]
        if not original_clean:
            continue

        route_points = build_day_route_points(
            amap_api_key=amap_api_key,
            destination=destination,
            day_item=day_item,
            max_points=max(3, len(original_clean)),
            max_segment_km=max_segment_km,
            destination_radius_km=destination_radius_km,
        )

        # 仅使用“真实可定位”来源替换，避免把 synthetic 近似点写回行程文本
        locatable_names = []
        seen = set()
        for p in route_points:
            if p.get("source") == "synthetic":
                continue
            name = normalize_spot_name(p.get("name", ""))
            if not name or name in seen:
                continue
            seen.add(name)
            locatable_names.append(name)

        # 候选不足时不强行替换，避免误伤
        if len(locatable_names) < 2:
            continue

        target_len = min(3, max(2, len(original_clean)))
        new_highlights = locatable_names[:target_len]

        if new_highlights != original_clean[:target_len]:
            replaced_count = sum(1 for idx in range(min(len(new_highlights), len(original_clean))) if new_highlights[idx] != original_clean[idx])
            if replaced_count > 0:
                day_item["highlights"] = new_highlights
                day_item["_auto_replaced"] = True
                days_updated += 1
                spots_replaced += replaced_count

    structured_data["daily_plan"] = daily_plan
    return {
        "updated": days_updated > 0,
        "days_updated": days_updated,
        "spots_replaced": spots_replaced,
    }

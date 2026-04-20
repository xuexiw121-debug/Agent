import json
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import dashscope
import folium
import streamlit as st
from streamlit_folium import st_folium

from services.config_service import (
    add_chinese_tiles,
    load_env_from_root,
    mask_key,
    normalize_destination_name,
    resolve_amap_api_key,
    resolve_dashscope_api_key,
    resolve_model_name,
)
from services.export_service import markdown_to_pdf_bytes, structured_plan_to_markdown
from services.llm_service import generate_travel_plan, run_health_check
from services.map_service import (
    build_day_route_snapshot_url,
    geocode_with_amap,
    repair_unlocatable_daily_highlights,
    render_day_route_map,
    render_multiday_route_map,
)

load_env_from_root(__file__)

DASHSCOPE_API_KEY = resolve_dashscope_api_key()
AMAP_API_KEY = resolve_amap_api_key()
MODEL_NAME = resolve_model_name()

dashscope.api_key = DASHSCOPE_API_KEY

st.set_page_config(page_title="AI 旅行规划师", page_icon="✈️")

HISTORY_PATH = Path(__file__).resolve().parent / ".streamlit" / "history_plans.json"


@st.cache_resource
def get_runtime_payload_store() -> dict:
    # 仅保存在服务进程内存中：可跨刷新恢复，服务重启后自动清空
    return {}


def get_or_create_visit_id() -> str:
    visit_id = ""
    try:
        raw = st.query_params.get("visit")
        if isinstance(raw, list):
            visit_id = str(raw[0]) if raw else ""
        elif raw is not None:
            visit_id = str(raw)
    except Exception:
        visit_id = ""

    if not visit_id:
        visit_id = uuid4().hex
        try:
            st.query_params["visit"] = visit_id
        except Exception:
            pass

    return visit_id


@st.cache_data(ttl=3600, show_spinner=False)
def build_base_pdf_bytes(markdown_text: str) -> bytes:
    return markdown_to_pdf_bytes(markdown_text, daily_route_maps=None)


def _load_history_store() -> dict:
    try:
        if not HISTORY_PATH.exists():
            return {}
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        # 新格式: {visit_id: [records]}
        if isinstance(data, dict):
            normalized = {}
            for k, v in data.items():
                if isinstance(v, list):
                    normalized[str(k)] = v
            return normalized
        # 兼容旧格式: [records]
        if isinstance(data, list):
            return {"__legacy_shared__": data}
        return {}
    except Exception:
        return {}


def _save_history_store(store: dict) -> None:
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(
            json.dumps(store, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_plan_history(visit_id: str) -> list:
    store = _load_history_store()

    if visit_id in store and isinstance(store.get(visit_id), list):
        return store.get(visit_id, [])

    legacy = store.get("__legacy_shared__")
    if isinstance(legacy, list) and legacy:
        # 首次读取时将旧格式历史迁移到当前访问，避免继续全局共享
        store[visit_id] = legacy
        store.pop("__legacy_shared__", None)
        _save_history_store(store)
        return store.get(visit_id, [])

    return []


def save_plan_history(items: list, visit_id: str) -> None:
    store = _load_history_store()
    store[visit_id] = items
    store.pop("__legacy_shared__", None)
    _save_history_store(store)


def append_plan_history(payload: dict, visit_id: str) -> None:
    history = load_plan_history(visit_id)
    snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
    record = {
        "id": uuid4().hex,
        "created_at": int(time.time()),
        "destination": snapshot.get("destination", "未知目的地"),
        "days": int(snapshot.get("days", 0) or 0),
        "total_budget": int(snapshot.get("total_budget", 0) or 0),
        "payload": snapshot,
    }
    history.insert(0, record)
    # 控制体量，避免历史文件过大
    save_plan_history(history[:20], visit_id)


def inject_ui_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg0: #0b1020;
            --bg1: #11182e;
            --panel: rgba(255,255,255,0.05);
            --panel-border: rgba(167, 139, 250, 0.25);
            --accent: #8b5cf6;
            --accent2: #38bdf8;
            --text: #e5e7eb;
            --muted: #9ca3af;
        }

        .stApp {
            background:
                radial-gradient(1200px 500px at 0% 0%, rgba(139,92,246,0.18) 0%, rgba(139,92,246,0) 70%),
                radial-gradient(900px 400px at 100% 10%, rgba(56,189,248,0.16) 0%, rgba(56,189,248,0) 70%),
                linear-gradient(180deg, var(--bg1) 0%, var(--bg0) 100%);
            color: var(--text);
        }

        .main > div {
            max-width: 1080px;
            padding-top: 1.2rem;
        }

        .hero-wrap {
            border: 1px solid rgba(167, 139, 250, 0.28);
            background: linear-gradient(135deg, rgba(139,92,246,0.16), rgba(56,189,248,0.12));
            box-shadow: 0 10px 36px rgba(0,0,0,0.28);
            border-radius: 20px;
            padding: 20px 22px;
            margin-bottom: 14px;
            backdrop-filter: blur(8px);
        }

        .hero-title {
            margin: 0;
            font-size: 32px;
            font-weight: 800;
            letter-spacing: -0.4px;
            background: linear-gradient(90deg, #c4b5fd, #7dd3fc 45%, #86efac);
            -webkit-background-clip: text;
            background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .hero-sub {
            margin: 8px 0 0;
            color: #d1d5db;
            font-size: 14px;
            line-height: 1.6;
        }

        .hero-badges {
            margin-top: 12px;
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }

        .hero-badge {
            font-size: 12px;
            color: #cbd5e1;
            padding: 4px 10px;
            border-radius: 999px;
            border: 1px solid rgba(203, 213, 225, 0.24);
            background: rgba(255,255,255,0.06);
        }

        div[data-testid="stExpander"] {
            border: 1px solid var(--panel-border);
            border-radius: 14px;
            background: var(--panel);
            backdrop-filter: blur(10px);
        }

        div[data-testid="stExpander"] details summary p {
            color: var(--text) !important;
            font-weight: 600;
        }

        div[data-testid="stForm"] {
            border: 1px solid var(--panel-border);
            border-radius: 16px;
            padding: 8px 10px;
            background: var(--panel);
        }

        div[data-testid="stMetric"] {
            border: 1px solid rgba(125, 211, 252, 0.18);
            border-radius: 12px;
            background: rgba(255,255,255,0.04);
            padding: 8px 10px;
        }

        .stButton > button, div[data-testid="stFormSubmitButton"] button, .stDownloadButton > button {
            border: 0 !important;
            border-radius: 10px !important;
            font-weight: 700 !important;
            background: linear-gradient(90deg, var(--accent), var(--accent2)) !important;
            color: #ffffff !important;
            box-shadow: 0 8px 22px rgba(56,189,248,0.22);
        }

        .stButton > button:hover, div[data-testid="stFormSubmitButton"] button:hover, .stDownloadButton > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 10px 28px rgba(139,92,246,0.35);
        }

        @media (max-width: 768px) {
            .hero-title { font-size: 26px; }
            .hero-wrap { padding: 16px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_ui_style()
st.markdown(
    """
    <div class="hero-wrap">
      <h1 class="hero-title">AI 智能旅行规划师</h1>
      <p class="hero-sub">结合大语言模型与地图路径能力，自动生成可执行的多日旅行方案，支持分段展示与报告导出。</p>
      <div class="hero-badges">
        <span class="hero-badge">路线可视化</span>
        <span class="hero-badge">不可定位自动替换</span>
        <span class="hero-badge">JSON/Markdown/PDF 导出</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if "generated_payload" not in st.session_state:
    st.session_state["generated_payload"] = None
if "stream_pending" not in st.session_state:
    st.session_state["stream_pending"] = False
if "restored_from_disk" not in st.session_state:
    st.session_state["restored_from_disk"] = False
if "visit_id" not in st.session_state:
    st.session_state["visit_id"] = get_or_create_visit_id()
if "confirm_clear_history" not in st.session_state:
    st.session_state["confirm_clear_history"] = False

if st.session_state["generated_payload"] is None:
    runtime_store = get_runtime_payload_store()
    restored_payload = runtime_store.get(st.session_state["visit_id"])
    if isinstance(restored_payload, dict) and restored_payload.get("plan"):
        st.session_state["generated_payload"] = restored_payload
        st.session_state["stream_pending"] = False
        st.session_state["restored_from_disk"] = True

with st.expander("🔎 运行诊断", expanded=False):
    st.write(f"当前模型: {MODEL_NAME}")
    st.write(f"API Key 状态: {mask_key(DASHSCOPE_API_KEY)}")
    st.write(f"高德 Key 状态: {mask_key(AMAP_API_KEY)}")
    if st.button("执行连通性测试", use_container_width=True):
        with st.spinner("正在测试与百炼服务的连通性..."):
            ok, msg = run_health_check(DASHSCOPE_API_KEY, MODEL_NAME)
        if ok:
            st.success(msg)
        else:
            st.error(msg)
    if st.button("执行高德地理编码测试", use_container_width=True):
        with st.spinner("正在测试高德地理编码..."):
            ok_geo, geo_msg = geocode_with_amap(AMAP_API_KEY, "北京市天安门")
        if ok_geo:
            st.success(f"高德连通成功: {geo_msg['formatted_address']} ({geo_msg['lat']}, {geo_msg['lon']})")
        else:
            st.error(str(geo_msg))

if st.session_state.get("restored_from_disk"):
    st.info("已恢复本次访问的上次生成结果。")

with st.sidebar.expander("🗂️ 历史方案库", expanded=True):
    history_items = load_plan_history(st.session_state["visit_id"])
    top_left, top_right = st.columns(2)
    top_left.caption(f"共 {len(history_items)} 条")
    if top_right.button("清空全部", key="clear_all_history_btn", use_container_width=True):
        st.session_state["confirm_clear_history"] = True

    if st.session_state.get("confirm_clear_history", False):
        st.warning("确认清空全部历史记录？此操作不可恢复。")
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button("确认清空", key="confirm_clear_all_history", use_container_width=True):
            save_plan_history([], st.session_state["visit_id"])
            st.session_state["confirm_clear_history"] = False
            st.rerun()
        if cancel_col.button("取消", key="cancel_clear_all_history", use_container_width=True):
            st.session_state["confirm_clear_history"] = False
            st.rerun()

    if not history_items:
        st.caption("暂无历史方案。")
    else:
        st.caption("左侧可快速恢复或删除历史方案。")
        for idx, item in enumerate(history_items):
            created_at = datetime.fromtimestamp(int(item.get("created_at", 0))).strftime("%m-%d %H:%M")
            destination_h = item.get("destination", "未知目的地")
            days_h = item.get("days", "?")
            budget_h = item.get("total_budget", "?")
            st.caption(f"{created_at} | {destination_h} | {days_h} 天 | ¥{budget_h}")
            c_restore, c_delete = st.columns(2)
            if c_restore.button("恢复", key=f"restore_history_{idx}", use_container_width=True):
                selected_payload = item.get("payload")
                if isinstance(selected_payload, dict) and selected_payload.get("plan"):
                    st.session_state["generated_payload"] = selected_payload
                    st.session_state["stream_pending"] = False
                    st.session_state["restored_from_disk"] = False
                    get_runtime_payload_store()[st.session_state["visit_id"]] = selected_payload
                    st.rerun()
            if c_delete.button("删除", key=f"delete_history_{idx}", use_container_width=True):
                delete_id = item.get("id")
                updated = [x for x in history_items if x.get("id") != delete_id]
                save_plan_history(updated, st.session_state["visit_id"])
                st.rerun()
            st.divider()

# 默认行为（移除高级设置面板后固定参数）
map_engine = "folium"
route_mode = "driving"
point_radius_px = 5
max_segment_km = 80
destination_radius_km = 120

enable_segment_stream = True
stream_delay = 0.12

generation_timeout_s = 90
enable_auto_fix = True
fix_time_budget_s = 6
fix_max_days = 3

with st.form("travel_form"):
    st.header("📝 请填写您的旅行需求")

    col1, col2 = st.columns(2)
    with col1:
        destination = st.text_input("目的地", placeholder="例如：上海、云南、大理、桂林、杭州...")
    with col2:
        preferences_options = ["历史文化", "自然风光", "美食探索", "购物娱乐", "亲子家庭", "冒险运动"]
        selected_preferences = st.multiselect("旅行偏好 (可选)", options=preferences_options)

    detailed_preferences = st.text_area("其他具体要求", placeholder="例如：预算中等，希望行程轻松一些，对艺术博物馆特别感兴趣...")

    col3, col4 = st.columns(2)
    with col3:
        days = st.number_input("旅行天数", min_value=1, max_value=21, value=5, step=1)
    with col4:
        total_budget = st.slider("总预算 (RMB)", min_value=1000, max_value=50000, value=8000, step=500)

    submitted = st.form_submit_button("🚀 生成我的专属旅行计划")

if submitted:
    full_preferences = ", ".join(selected_preferences)
    if detailed_preferences:
        full_preferences += f" | {detailed_preferences}"

    normalized_destination = normalize_destination_name(destination)

    if not normalized_destination:
        st.warning("请输入您的目的地！")
    else:
        if normalized_destination != destination.strip():
            st.info(f"已将目的地标准化为：{normalized_destination}")

        progress = st.progress(0, text="步骤 1/4：准备输入参数...")
        progress.progress(15, text="步骤 2/4：定位目的地...")
        ok_geo, geo_data = geocode_with_amap(AMAP_API_KEY, normalized_destination)

        progress.progress(35, text="步骤 3/4：调用大模型生成行程...")
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    generate_travel_plan,
                    dashscope_api_key=DASHSCOPE_API_KEY,
                    model_name=MODEL_NAME,
                    destination=normalized_destination,
                    preferences=full_preferences,
                    days=int(days),
                    total_budget=int(total_budget),
                )
                plan, err = future.result(timeout=float(generation_timeout_s))
        except FuturesTimeoutError:
            plan, err = None, f"AI 生成超时（>{generation_timeout_s} 秒），请重试或减少天数。"
        except Exception as e:
            plan, err = None, f"生成异常: {e}"

        if err:
            progress.empty()
            st.error(err)
        elif plan:
            fix_summary = {"updated": False, "days_updated": 0, "spots_replaced": 0}
            effective_auto_fix = enable_auto_fix and int(days) <= 7
            if plan.get("structured") and effective_auto_fix:
                progress.progress(70, text="步骤 4/4：校验并纠偏景点定位...")
                with st.spinner("正在校验并替换不可定位景点..."):
                    fix_summary = repair_unlocatable_daily_highlights(
                        amap_api_key=AMAP_API_KEY,
                        destination=normalized_destination,
                        structured_data=plan["structured"],
                        max_segment_km=float(max_segment_km),
                        destination_radius_km=float(destination_radius_km),
                        max_days_to_fix=int(fix_max_days),
                        time_budget_s=float(fix_time_budget_s),
                    )
            elif int(days) > 7:
                st.info("行程天数较多，已跳过自动纠偏以优先保证生成速度。")

            progress.progress(95, text="步骤 4/4：保存结果并准备展示...")

            st.session_state["generated_payload"] = {
                "destination": normalized_destination,
                "preferences": full_preferences,
                "days": int(days),
                "total_budget": int(total_budget),
                "plan": plan,
                "geo_ok": bool(ok_geo),
                "geo_data": geo_data,
                "fix_summary": fix_summary,
            }
            st.session_state["stream_pending"] = True
            st.session_state["restored_from_disk"] = False
            get_runtime_payload_store()[st.session_state["visit_id"]] = st.session_state["generated_payload"]
            append_plan_history(st.session_state["generated_payload"], st.session_state["visit_id"])

            progress.progress(100, text="生成完成")
            time.sleep(0.15)
            progress.empty()
            st.rerun()


def render_day_plan_section(
    *,
    day_item: dict,
    amap_api_key: str,
    destination: str,
    point_radius_px: int,
    map_engine: str,
    max_segment_km: float,
    route_mode: str,
    destination_radius_km: float,
) -> None:
    title = f"Day {day_item.get('day', '?')} - {day_item.get('theme', '未命名主题')}"
    with st.expander(title, expanded=False):
        highlights = day_item.get("highlights", [])
        if highlights:
            st.write("亮点：")
            for h in highlights:
                st.write(f"- {h}")
        st.write(f"餐饮建议：{day_item.get('food', '(无)')}")
        st.write(f"交通建议：{day_item.get('transport', '(无)')}")
        st.write(f"预计花费：¥{day_item.get('estimated_cost', 0)}")
        st.markdown("**当日路线图（高德地理编码）**")
        render_day_route_map(
            amap_api_key=amap_api_key,
            destination=destination,
            day_item=day_item,
            point_radius_px=point_radius_px,
            map_engine=map_engine,
            max_segment_km=max_segment_km,
            route_mode=route_mode,
            destination_radius_km=destination_radius_km,
        )

payload = st.session_state.get("generated_payload")
if payload:
    destination_saved = payload["destination"]
    plan_saved = payload["plan"]
    geo_ok_saved = payload.get("geo_ok", False)
    geo_data_saved = payload.get("geo_data")
    fix_summary = payload.get("fix_summary", {})

    st.header("✨ 为您生成的旅行计划")
    st.caption(f"当前展示：{destination_saved} | {payload.get('days', '?')} 天 | 预算 ¥{payload.get('total_budget', '?')}")

    if geo_ok_saved and isinstance(geo_data_saved, dict):
        st.subheader("📍 目的地地图")
        st.caption(f"{geo_data_saved['formatted_address']} | {geo_data_saved['province']} {geo_data_saved['city']} {geo_data_saved['district']}")
        m_dest = folium.Map(
            location=[geo_data_saved["lat"], geo_data_saved["lon"]],
            zoom_start=11,
            tiles=None,
            control_scale=True,
        )
        add_chinese_tiles(m_dest)
        folium.Marker(
            location=[geo_data_saved["lat"], geo_data_saved["lon"]],
            popup=destination_saved,
            tooltip=destination_saved,
        ).add_to(m_dest)
        st_folium(m_dest, use_container_width=True, height=300, returned_objects=[], key="dest_map")
    elif geo_data_saved:
        st.warning(f"高德地图定位失败，将继续展示攻略文本：{geo_data_saved}")

    budget = plan_saved["budget"]
    alloc = budget["allocation"]
    st.subheader("💰 预算分配")
    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("交通", f"¥{alloc['交通']}")
    b2.metric("住宿", f"¥{alloc['住宿']}")
    b3.metric("餐饮", f"¥{alloc['餐饮']}")
    b4.metric("景点活动", f"¥{alloc['景点活动']}")
    b5.metric("机动", f"¥{alloc['机动']}")

    if fix_summary.get("updated"):
        st.info(
            f"已自动替换 {fix_summary.get('days_updated', 0)} 天中无法定位的景点，共替换 {fix_summary.get('spots_replaced', 0)} 处。"
        )

    if plan_saved["structured"]:
        data = plan_saved["structured"]
        st.subheader("🗺️ 行程总览")
        st.info(data.get("overview", "(无)"))
        st.write(f"预算策略：{data.get('budget_summary', '(无)')}")

        st.subheader("📅 每日安排")
        daily_plan = data.get("daily_plan", [])
        stream_pending = bool(st.session_state.get("stream_pending", False))
        if enable_segment_stream and stream_pending and isinstance(daily_plan, list) and daily_plan:
            ordered_plan = sorted(
                daily_plan,
                key=lambda x: int(x.get("day")) if isinstance(x.get("day"), int) else 999,
            )
            progress = st.progress(0, text="正在分段加载每日行程...")
            for idx, day_item in enumerate(ordered_plan, start=1):
                render_day_plan_section(
                    day_item=day_item,
                    amap_api_key=AMAP_API_KEY,
                    destination=destination_saved,
                    point_radius_px=point_radius_px,
                    map_engine=map_engine,
                    max_segment_km=float(max_segment_km),
                    route_mode=route_mode,
                    destination_radius_km=float(destination_radius_km),
                )
                progress.progress(idx / len(ordered_plan), text=f"已加载 Day {idx}/{len(ordered_plan)}")
                if stream_delay > 0:
                    time.sleep(float(stream_delay))
            progress.empty()
            st.session_state["stream_pending"] = False
        else:
            for day_item in daily_plan:
                render_day_plan_section(
                    day_item=day_item,
                    amap_api_key=AMAP_API_KEY,
                    destination=destination_saved,
                    point_radius_px=point_radius_px,
                    map_engine=map_engine,
                    max_segment_km=float(max_segment_km),
                    route_mode=route_mode,
                    destination_radius_km=float(destination_radius_km),
                )

        st.subheader("🧭 多日总览路线（按天分段）")
        st.caption("总览图复用各天路线计算结果并进行合并展示。")
        available_days = sorted(
            {
                int(item.get("day"))
                for item in data.get("daily_plan", [])
                if isinstance(item.get("day"), int)
            }
        )
        selected_days = st.multiselect(
            "选择显示天数",
            options=available_days,
            default=available_days,
            format_func=lambda d: f"Day {d}",
            key="multiday_filter_days",
        )

        render_multiday_route_map(
            amap_api_key=AMAP_API_KEY,
            destination=destination_saved,
            structured_data=data,
            point_radius_px=point_radius_px,
            map_engine=map_engine,
            selected_days=selected_days,
            max_segment_km=float(max_segment_km),
            route_mode=route_mode,
            destination_radius_km=float(destination_radius_km),
            fast_mode=False,
        )

        tips = data.get("tips", [])
        if tips:
            st.subheader("✅ 出行提示")
            for tip in tips:
                st.write(f"- {tip}")

        st.download_button(
            label="📥 下载计划为 JSON 文件",
            data=json.dumps(data, ensure_ascii=False, indent=2),
            file_name=f"travel_plan_{destination_saved.replace(' ', '_')}.json",
            mime="application/json",
        )

        md_text = structured_plan_to_markdown(
            destination=destination_saved,
            days=int(payload.get("days", 0)),
            total_budget=int(payload.get("total_budget", 0)),
            data=data,
        )
        st.download_button(
            label="📄 下载可读版 Markdown",
            data=md_text,
            file_name=f"travel_plan_{destination_saved.replace(' ', '_')}.md",
            mime="text/markdown",
        )

        pdf_file_name = f"travel_plan_{destination_saved.replace(' ', '_')}.pdf"
        pdf_key_seed = (
            f"{destination_saved}|{md_text}|{route_mode}|"
            f"{float(max_segment_km):.2f}|{float(destination_radius_km):.2f}"
        )
        pdf_cache_key = hashlib.sha1(pdf_key_seed.encode("utf-8")).hexdigest()

        if st.session_state.get("pdf_cache_key") != pdf_cache_key:
            st.session_state["pdf_cache_key"] = pdf_cache_key
            st.session_state["pdf_cached_bytes"] = None
            st.session_state["pdf_cached_label"] = "🧾 下载 PDF（含路线图）"
            st.session_state["pdf_cached_error"] = None

        if st.session_state.get("pdf_cached_bytes") is not None:
            st.download_button(
                label=st.session_state.get("pdf_cached_label", "🧾 下载 PDF（含路线图）"),
                data=st.session_state["pdf_cached_bytes"],
                file_name=pdf_file_name,
                mime="application/pdf",
            )
            if st.session_state.get("pdf_cached_error"):
                st.caption(st.session_state["pdf_cached_error"])
        else:
            st.download_button(
                label="🧾 下载 PDF（含路线图）",
                data=b"",
                file_name=pdf_file_name,
                mime="application/pdf",
                disabled=True,
                help="PDF 正在生成中，请稍候...",
            )
            st.caption("正在生成 PDF（含路线图），完成后按钮将自动可点击。")
            daily_plan_items = data.get("daily_plan", [])
            total_steps = max(1, len(daily_plan_items) + 1)
            pdf_progress = st.progress(0, text="正在准备 PDF 内容...")
            try:
                daily_route_maps = []
                for idx, day_item in enumerate(daily_plan_items, start=1):
                    day_no = day_item.get("day", idx)
                    pdf_progress.progress(
                        int((idx / total_steps) * 100),
                        text=f"正在处理 Day {day_no} 路线图（{idx}/{len(daily_plan_items)}）...",
                    )
                    image_url = build_day_route_snapshot_url(
                        amap_api_key=AMAP_API_KEY,
                        destination=destination_saved,
                        day_item=day_item,
                        max_segment_km=float(max_segment_km),
                        route_mode=route_mode,
                        destination_radius_km=float(destination_radius_km),
                    )
                    if image_url:
                        daily_route_maps.append(
                            {
                                "day": day_item.get("day", "?"),
                                "theme": day_item.get("theme", ""),
                                "spots": day_item.get("highlights", []),
                                "image_url": image_url,
                            }
                        )

                pdf_progress.progress(95, text="正在排版并生成 PDF 文件...")
                st.session_state["pdf_cached_bytes"] = markdown_to_pdf_bytes(
                    md_text,
                    daily_route_maps=daily_route_maps,
                )
                st.session_state["pdf_cached_label"] = "🧾 下载 PDF（含路线图）"
                st.session_state["pdf_cached_error"] = None
                pdf_progress.progress(100, text="PDF 已生成，正在刷新下载按钮...")
            except Exception as e:
                st.session_state["pdf_cached_bytes"] = build_base_pdf_bytes(md_text)
                st.session_state["pdf_cached_label"] = "🧾 下载 PDF"
                st.session_state["pdf_cached_error"] = f"含路线图生成失败，已回退基础 PDF。原因: {e}"
                pdf_progress.progress(100, text="路线图生成失败，已回退基础 PDF。")
            st.rerun()
    else:
        st.subheader("📄 原始结果")
        st.markdown(plan_saved["raw"])

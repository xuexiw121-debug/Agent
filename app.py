import json
import time

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
st.title("🌍 AI 智能旅行规划师")
st.caption("由 Streamlit 和阿里云百联强力驱动，为您打造专属旅行计划！")

if "generated_payload" not in st.session_state:
    st.session_state["generated_payload"] = None
if "stream_pending" not in st.session_state:
    st.session_state["stream_pending"] = False

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

with st.expander("🗺️ 地图显示设置", expanded=False):
    map_engine_label = st.radio(
        "地图引擎",
        options=["Folium（推荐稳定）", "Pydeck（实验）"],
        index=0,
        horizontal=True,
    )
    map_engine = "folium" if map_engine_label.startswith("Folium") else "pydeck"

    route_mode_label = st.radio(
        "通行方式",
        options=["驾车", "步行"],
        index=0,
        horizontal=True,
    )
    route_mode = "driving" if route_mode_label == "驾车" else "walking"

    point_radius_px = st.slider("景点点位大小", min_value=2, max_value=12, value=5, step=1)
    max_segment_km = st.slider("相邻景点最大距离(km)", min_value=5, max_value=120, value=80, step=5)
    destination_radius_km = st.slider("目的地约束半径(km)", min_value=20, max_value=300, value=120, step=10)

with st.expander("⚡ 展示体验设置", expanded=False):
    enable_segment_stream = st.toggle("按天分段流式展示", value=True)
    stream_delay = st.slider("每段加载间隔(秒)", min_value=0.0, max_value=0.8, value=0.12, step=0.02)

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

        ok_geo, geo_data = geocode_with_amap(AMAP_API_KEY, normalized_destination)

        with st.spinner("AI 正在紧张地为您规划行程中，请稍候..."):
            plan, err = generate_travel_plan(
                dashscope_api_key=DASHSCOPE_API_KEY,
                model_name=MODEL_NAME,
                destination=normalized_destination,
                preferences=full_preferences,
                days=int(days),
                total_budget=int(total_budget),
            )

        if err:
            st.error(err)
        elif plan:
            fix_summary = {"updated": False, "days_updated": 0, "spots_replaced": 0}
            if plan.get("structured"):
                fix_summary = repair_unlocatable_daily_highlights(
                    amap_api_key=AMAP_API_KEY,
                    destination=normalized_destination,
                    structured_data=plan["structured"],
                    max_segment_km=float(max_segment_km),
                    destination_radius_km=float(destination_radius_km),
                )

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

        st.subheader("🧭 多日总览路线（按天分段）")
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
        )

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

        try:
            daily_route_maps = []
            for day_item in data.get("daily_plan", []):
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

            pdf_bytes = markdown_to_pdf_bytes(md_text, daily_route_maps=daily_route_maps)
            st.download_button(
                label="🧾 下载 PDF",
                data=pdf_bytes,
                file_name=f"travel_plan_{destination_saved.replace(' ', '_')}.pdf",
                mime="application/pdf",
            )
        except Exception as e:
            st.warning(f"PDF 生成失败，可先下载 Markdown。原因: {e}")
    else:
        st.subheader("📄 原始结果")
        st.markdown(plan_saved["raw"])

# Streamlit Cloud Deployment Guide

## 1. Push code to GitHub
1. Create a GitHub repository.
2. Push the `streamlit-travel-planner` folder content.
3. Make sure these files exist in repository root:
- `app.py`
- `requirements.txt`
- `.streamlit/config.toml`

## 2. Create app on Streamlit Cloud
1. Open Streamlit Community Cloud.
2. Click `New app`.
3. Select your GitHub repository and branch.
4. Set `Main file path` to `app.py`.
5. Click `Deploy`.

## 3. Configure secrets
In Streamlit Cloud app settings, open `Secrets` and paste:

```toml
DASHSCOPE_API_KEY = "your_dashscope_api_key"
AMAP_API_KEY = "your_amap_api_key"
DASHSCOPE_MODEL = "qwen3-max"
```

Then save and reboot the app.

## 4. Verify after deployment
1. Open app and run `执行连通性测试` in diagnostics section.
2. Run `执行高德地理编码测试`.
3. Generate a sample trip plan and test PDF export.

## 5. Notes
- PDF Chinese support is handled with a Linux-compatible CJK font fallback in code.
- Never put real keys in `.streamlit/secrets.toml.example`.
- If deployment fails, check app logs in Streamlit Cloud for missing package or key errors.

# BV8 / TAX API GitHub Actions 签到

已从前端代码和浏览器抓包确认：

## 接口

登录：

```text
POST https://api.bv8.my/api/user/login?turnstile=
Content-Type: application/json

{"username":"你的用户名","password":"你的密码"}
```

签到：

```text
POST https://api.bv8.my/api/user/checkin
```

签到成功响应示例：

```json
{"success": true, "message": "签到成功", "data": {"quota_awarded": 959692}}
```

随后页面会请求：

```text
GET https://api.bv8.my/api/user/checkin?month=YYYY-MM
```

当前站点配置里 `turnstile_check=false`，所以 GitHub Actions 可以用账号密码登录获取会话 Cookie，再签到。Cookie 不需要写死。

## 放到仓库

复制这两个文件到你的仓库：

- `scripts/bv8_checkin.py` -> 你的仓库 `scripts/bv8_checkin.py`
- `.github/workflows/bv8_checkin.yml` -> 你的仓库 `.github/workflows/bv8_checkin.yml`

## 配置 GitHub Secrets

在 GitHub 仓库：

```text
Settings -> Secrets and variables -> Actions -> New repository secret
```

必填：

- `BV8_USERNAME`：你的登录用户名
- `BV8_PASSWORD`：你的登录密码

可选：

- `BV8_TOTP_SECRET`：如果账号开启了 2FA，填认证器的 base32 密钥，脚本会自动生成 6 位 TOTP
- `BV8_2FA_CODE`：手动运行时可临时填一次性验证码/备用码
- `BV8_COOKIE`：仅作为备用，不推荐长期使用，因为会过期

## 运行时间

workflow 默认每天 `16:10 UTC` 运行，即北京时间次日 `00:10` 左右。也可以在 Actions 页面手动 `Run workflow`。

## 注意

- 不要把账号、密码、Cookie、TOTP 密钥提交到代码。
- 如果以后站点启用 Turnstile，人机验证 token 可能无法在 GitHub Actions 里纯脚本生成；届时需要换成可自动刷新 token 的方案，或继续使用 Cookie/浏览器自动化方案。

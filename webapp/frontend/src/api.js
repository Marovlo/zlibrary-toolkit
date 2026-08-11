/** 极简 fetch 封装。出错时抛出 Error(message)，message 取后端返回的友好提示
 * （FastAPI HTTPException 的 detail 字段），网络层错误不暴露任何实现细节。 */
const BASE = "/api";

async function request(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText || "请求失败";
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (e) {
      // ignore
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  listAccounts: () => request("GET", "/accounts"),
  addAccount: (email, password) => request("POST", "/accounts", { email, password }),
  search: (query, page, account_email, force_refresh = false) =>
    request("POST", "/search", { query, page, account_email, force_refresh }),
  startDownload: (payload) => request("POST", "/download", payload),
  getJob: (jobId) => request("GET", `/download/${jobId}`),
  listArchive: (q = "") => request("GET", `/archive${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  deleteArchive: (id) => request("DELETE", `/archive/${id}`),
  archiveFileUrl: (id) => `${BASE}/archive/${id}/file`,
  getStatus: (refresh = false) => request("GET", `/status${refresh ? "?refresh=true" : ""}`),
};

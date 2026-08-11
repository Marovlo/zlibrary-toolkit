<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const status = ref(null);
const cookies = ref("");
const adding = ref(false);
const errorMsg = ref("");

async function refresh() {
  status.value = await api.getBaidu();
}

async function addCookies() {
  if (!cookies.value.trim()) return;
  adding.value = true;
  errorMsg.value = "";
  try {
    status.value = await api.addBaidu(cookies.value);
    cookies.value = "";
  } catch (e) {
    errorMsg.value = e.message;
  } finally {
    adding.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <section class="baidu">
    <h2>百度网盘</h2>
    <div v-if="status" class="status-card">
      <div class="row">
        <span class="label">状态</span>
        <span v-if="status.logged_in" class="ok">✓ 已登录</span>
        <span v-else-if="status.configured" class="warn">已配置但未登录</span>
        <span v-else class="muted">未配置</span>
      </div>
      <div class="row" v-if="status.account">
        <span class="label">账号</span>
        <span>{{ status.account }}</span>
      </div>
      <div class="row">
        <span class="label">BaiduPCS-Go</span>
        <span>{{ status.binary_version || "未安装" }}</span>
      </div>
    </div>

    <h3>添加 / 更新 cookies</h3>
    <p class="hint">
      获取方法：浏览器登录 <code>pan.baidu.com</code> → F12 → Application → Cookies →
      复制整段 Cookie 字符串（含 BDUSS 和 STOKEN）。会先验证能否登录，成功才保存。
    </p>
    <form @submit.prevent="addCookies">
      <textarea
        v-model="cookies"
        placeholder="粘贴 cookies 字符串..."
        rows="3"
        class="cookies-input"
      ></textarea>
      <button type="submit" :disabled="adding">
        {{ adding ? "验证中..." : "保存" }}
      </button>
    </form>
    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
  </section>
</template>

<style scoped>
.status-card {
  background: #f9f9f9;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  padding: 12px 16px;
  margin-bottom: 20px;
}
.row {
  display: flex;
  gap: 12px;
  padding: 4px 0;
}
.row .label {
  min-width: 110px;
  color: #666;
  font-size: 13px;
}
.ok { color: #2c8a3a; font-weight: 600; }
.warn { color: #c77e00; font-weight: 600; }
.muted { color: #999; }
.cookies-input {
  width: 100%;
  max-width: 600px;
  font-family: monospace;
  font-size: 12px;
  padding: 8px;
  border: 1px solid #ccc;
  border-radius: 4px;
  resize: vertical;
}
.hint {
  color: #666;
  font-size: 13px;
  line-height: 1.6;
}
.hint code {
  background: #f0f0f0;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
</style>

<script setup>
import { onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { api } from "../api";

const accounts = ref([]);
const summary = ref(null);
const newEmail = ref("");
const newPassword = ref("");
const adding = ref(false);
const registering = ref(false);
const registrationJob = ref(null);
const errorMsg = ref("");
let refreshTimer = null;
let registrationTimer = null;

async function refresh() {
  try {
    accounts.value = await api.listAccounts();
    summary.value = await api.getAccountSummary();
  } catch (e) {
    errorMsg.value = e.message;
  }
}

async function refreshRegistration() {
  const jobId = registrationJob.value?.id || summary.value?.registration_job_id;
  if (!jobId) return;
  try {
    registrationJob.value = await api.getAccountRegistration(jobId);
    if (["success", "failed"].includes(registrationJob.value.status)) {
      stopRegistrationPolling();
      await refresh();
    }
  } catch (e) {
    errorMsg.value = e.message;
    stopRegistrationPolling();
  }
}

function startRegistrationPolling() {
  if (registrationTimer) return;
  refreshRegistration();
  registrationTimer = setInterval(refreshRegistration, 2000);
}

function stopRegistrationPolling() {
  if (registrationTimer) {
    clearInterval(registrationTimer);
    registrationTimer = null;
  }
}

function startAutoRefresh() {
  if (refreshTimer) return;
  refresh();
  refreshTimer = setInterval(refresh, 10000);
  if (summary.value?.registration_job_id || registrationJob.value) {
    startRegistrationPolling();
  }
}

function stopAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  stopRegistrationPolling();
}

async function startRegistration() {
  if (registering.value || registrationJob.value?.status === "running") return;
  registering.value = true;
  errorMsg.value = "";
  try {
    registrationJob.value = await api.startAccountRegistration();
    startRegistrationPolling();
  } catch (e) {
    errorMsg.value = e.message;
  } finally {
    registering.value = false;
  }
}

async function addAccount() {
  if (!newEmail.value || !newPassword.value) return;
  adding.value = true;
  errorMsg.value = "";
  try {
    await api.addAccount(newEmail.value, newPassword.value);
    newEmail.value = "";
    newPassword.value = "";
    await refresh();
  } catch (e) {
    errorMsg.value = e.message;
  } finally {
    adding.value = false;
  }
}

onMounted(startAutoRefresh);
onActivated(startAutoRefresh);
onDeactivated(stopAutoRefresh);
onUnmounted(stopAutoRefresh);
</script>

<template>
  <section class="accounts">
    <h2>账号池</h2>
    <div v-if="summary" class="quota-summary" :class="{ warning: summary.low_balance }">
      <span>账号池剩余额度约 {{ summary.total_remaining }} 本（{{ summary.available_accounts }} 个可用账号）</span>
      <span v-if="summary.low_balance" class="quota-warning">
        额度较低，可人工申请新账号
      </span>
    </div>
    <table>
      <thead>
        <tr><th>邮箱</th><th>今日已下载</th><th>剩余</th><th>可用</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in accounts" :key="a.email">
          <td>{{ a.email }}</td>
          <td>{{ a.downloads_today }} / {{ a.limit }}</td>
          <td>{{ a.remaining ?? `${a.effective_remaining}（估算）` }}</td>
          <td>{{ a.available ? "✓" : "已用尽" }}</td>
        </tr>
        <tr v-if="!accounts.length"><td colspan="4">暂无账号，可在下方添加</td></tr>
      </tbody>
    </table>

    <div v-if="summary?.low_balance" class="registration-panel">
      <button
        type="button"
        class="register-btn"
        :disabled="registering || ['pending', 'running'].includes(registrationJob?.status)"
        @click="startRegistration"
      >
        {{ registering || ['pending', 'running'].includes(registrationJob?.status) ? "注册进行中..." : "一键注册新账号" }}
      </button>
      <span class="hint">注册在后台执行，切换页面不会中断。</span>
      <div v-if="registrationJob" class="registration-progress">
        <strong>{{ registrationJob.email || "正在生成邮箱" }}</strong>
        <span v-if="registrationJob.status === 'success'" class="success">{{ registrationJob.message }}</span>
        <span v-else-if="registrationJob.status === 'failed'" class="error">{{ registrationJob.error || registrationJob.message }}</span>
        <span v-else class="hint">{{ registrationJob.message }}</span>
      </div>
    </div>

    <h3>添加已有账号</h3>
    <p class="hint">会先做一次真实登录测试，成功才会保存（跟 CLI 的 `zlib add-account` 行为一致）。</p>
    <form @submit.prevent="addAccount">
      <input v-model="newEmail" placeholder="邮箱" />
      <input v-model="newPassword" type="password" placeholder="密码" />
      <button type="submit" :disabled="adding">{{ adding ? "验证中..." : "添加" }}</button>
    </form>
    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
  </section>
</template>

<style scoped>
.quota-summary {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  margin: 10px 0;
  background: #eef8ee;
  border: 1px solid #b9dfb9;
  border-radius: 4px;
}
.quota-summary.warning {
  background: #fff7e6;
  border-color: #f0c36d;
}
.quota-warning {
  color: #b26a00;
  font-weight: 600;
}
.registration-panel {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 0 20px;
}
.register-btn {
  background: #2c7be5;
  color: #fff;
  border: none;
  padding: 7px 14px;
  border-radius: 4px;
  cursor: pointer;
}
.register-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.registration-progress {
  flex-basis: 100%;
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.success {
  color: #188038;
}
</style>

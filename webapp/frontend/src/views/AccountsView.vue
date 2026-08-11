<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const accounts = ref([]);
const newEmail = ref("");
const newPassword = ref("");
const adding = ref(false);
const errorMsg = ref("");

async function refresh() {
  accounts.value = await api.listAccounts();
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

onMounted(refresh);
</script>

<template>
  <section class="accounts">
    <h2>账号池</h2>
    <table>
      <thead>
        <tr><th>邮箱</th><th>今日已下载</th><th>剩余</th><th>可用</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in accounts" :key="a.email">
          <td>{{ a.email }}</td>
          <td>{{ a.downloads_today }} / {{ a.limit }}</td>
          <td>{{ a.remaining ?? "未知" }}</td>
          <td>{{ a.available ? "✓" : "已用尽" }}</td>
        </tr>
        <tr v-if="!accounts.length"><td colspan="4">暂无账号，可在下方添加</td></tr>
      </tbody>
    </table>

    <h3>添加账号</h3>
    <p class="hint">会先做一次真实登录测试，成功才会保存（跟 CLI 的 `zlib add-account` 行为一致）。</p>
    <form @submit.prevent="addAccount">
      <input v-model="newEmail" placeholder="邮箱" />
      <input v-model="newPassword" type="password" placeholder="密码" />
      <button type="submit" :disabled="adding">{{ adding ? "验证中..." : "添加" }}</button>
    </form>
    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
  </section>
</template>

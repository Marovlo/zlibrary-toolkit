<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../api";

const state = ref({ status: "initializing" });
let timer = null;

// 探测中/切换中状态要更快感知恢复情况，稳定态（direct/proxy_ok）则放慢轮询。
const FAST_STATES = new Set(["initializing", "connecting", "switching"]);

async function load() {
  try {
    state.value = await api.getStatus();
  } catch (e) {
    state.value = { status: "unavailable", error: "网络暂时不稳定" };
  }
  schedule();
}

function schedule() {
  if (timer) clearTimeout(timer);
  const interval = FAST_STATES.has(state.value.status) ? 3000 : 30000;
  timer = setTimeout(load, interval);
}

onMounted(load);
onUnmounted(() => timer && clearTimeout(timer));
</script>

<template>
  <div class="status-badge" :class="state.status">
    <span class="dot"></span>
    <span v-if="state.status === 'initializing' || state.status === 'connecting'">
      {{ state.message || "正在选择可用代理..." }}
    </span>
    <span v-else-if="state.status === 'direct'">直连可用</span>
    <span v-else-if="state.status === 'proxy_ok'">代理: {{ state.node || "已连接" }}</span>
    <span v-else-if="state.status === 'switching'">节点异常，正在切换...</span>
    <span v-else-if="state.status === 'unavailable'">{{ state.error || "暂无可用节点" }}</span>
    <span v-else>网络状态未知</span>
  </div>
</template>

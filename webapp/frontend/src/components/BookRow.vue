<script setup>
import { onUnmounted, ref } from "vue";
import { api } from "../api";

const props = defineProps({
  book: { type: Object, required: true },
  accountEmail: { type: String, default: "" },
});

const status = ref("idle"); // idle|running|success|failed
const message = ref("");
const errorMsg = ref("");
const archivedId = ref(null);
let timer = null;
let autoSaved = false; // 确保同一次下载只自动触发一次保存

// 下载状态轮询：不需要很实时，2.5秒一次足够，避免请求过于频繁（也避免后台日志刷屏）。
const POLL_INTERVAL = 2500;

function stopPoll() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}

/** 下载完成后不再需要用户手动点击链接确认——直接用隐藏的 <a> 触发浏览器保存到
 * 本地。后端文件接口带 Content-Disposition: attachment，浏览器会当成文件下载
 * 而不是跳转页面，同源请求也不会被当成弹窗拦截。*/
function autoSave(id) {
  if (autoSaved) return;
  autoSaved = true;
  const a = document.createElement("a");
  a.href = api.archiveFileUrl(id);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function poll(jobId) {
  try {
    const job = await api.getJob(jobId);
    status.value = job.status;
    message.value = job.message;
    if (job.status === "success") {
      archivedId.value = job.archived_id;
      autoSave(job.archived_id);
      stopPoll();
      return;
    }
    if (job.status === "failed") {
      errorMsg.value = job.error || "下载失败";
      stopPoll();
      return;
    }
    timer = setTimeout(() => poll(jobId), POLL_INTERVAL);
  } catch (e) {
    errorMsg.value = "下载失败";
    status.value = "failed";
    stopPoll();
  }
}

async function startDownload() {
  status.value = "running";
  errorMsg.value = "";
  message.value = "正在提交下载任务...";
  autoSaved = false;
  try {
    const job = await api.startDownload({
      book_id: props.book.book_id,
      hash: props.book.hash,
      title: props.book.title,
      author: props.book.author,
      year: props.book.year,
      language: props.book.language,
      format: props.book.format,
      size: props.book.size,
      rating: props.book.rating,
      detail_url: props.book.detail_url,
      download_url: props.book.download_url,
      account_email: props.accountEmail,
    });
    status.value = job.status;
    message.value = job.message;
    if (job.status === "success") {
      archivedId.value = job.archived_id;
      autoSave(job.archived_id);
      return;
    }
    if (job.status === "failed") {
      errorMsg.value = job.error || "下载失败";
      return;
    }
    timer = setTimeout(() => poll(job.id), POLL_INTERVAL);
  } catch (e) {
    status.value = "failed";
    errorMsg.value = e.message;
  }
}

function retry() {
  status.value = "idle";
  errorMsg.value = "";
}

onUnmounted(stopPoll);
</script>

<template>
  <div class="book-row">
    <div class="info">
      <div class="title">
        {{ book.title }}
        <span v-if="book.match_score === 100" class="badge full">完全匹配</span>
        <span v-else-if="book.match_score >= 90" class="badge prefix">前缀匹配</span>
      </div>
      <div class="meta">
        {{ book.author || "未知作者" }} · {{ book.year || "-" }} ·
        {{ (book.format || "?").toUpperCase() }} · {{ book.size || "-" }} ·
        评分 {{ book.rating || "-" }}
      </div>
    </div>
    <div class="action">
      <button v-if="status === 'idle'" @click="startDownload">下载</button>
      <span v-else-if="status === 'running'" class="hint downloading">
        <span class="spinner"></span>{{ message || "处理中..." }}
      </span>
      <span v-else-if="status === 'success'" class="hint success">
        已自动保存到本地
        <a :href="api.archiveFileUrl(archivedId)" target="_blank">（重新保存）</a>
      </span>
      <span v-else-if="status === 'failed'">
        <span class="error">{{ errorMsg }}</span>
        <button @click="retry">重试</button>
      </span>
    </div>
  </div>
</template>

<script setup>
import { onUnmounted, ref } from "vue";
import { api } from "../api";

const props = defineProps({
  book: { type: Object, required: true },
  accountEmail: { type: String, default: "" },
});

const status = ref("idle"); // idle|running|success|failed（pending 在前端按 running 展示）
const phase = ref("");      // downloading|uploading|done|submitting
const message = ref("");
const errorMsg = ref("");
const archivedId = ref(null);
const shareUrl = ref("");
const copied = ref(false);
let timer = null;

// 下载状态轮询：不需要很实时，2.5秒一次足够，避免请求过于频繁（也避免后台日志刷屏）。
const POLL_INTERVAL = 2500;

function stopPoll() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}

/** 触发浏览器下载（点击隐藏 <a>，后端带 Content-Disposition: attachment） */
function downloadLocal(id) {
  const a = document.createElement("a");
  a.href = api.archiveFileUrl(id);
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function copyShare() {
  if (!shareUrl.value) return;
  try {
    await navigator.clipboard.writeText(shareUrl.value);
    copied.value = true;
    setTimeout(() => { copied.value = false; }, 2000);
  } catch (e) {
    // 降级：选中文本让用户手动复制
    prompt("复制分享链接：", shareUrl.value);
  }
}

// 后端 job 刚提交时是 "pending"（线程刚入队、还没跑到下载阶段）。这段时间若直接
// 用 "pending" 渲染，会匹配不到任何模板分支而显示空白。统一当成"提交中"的 running
// 态展示，直到后端给出 running/success/failed，避免出现"卡一下"的空白间隙。
function applyJob(job) {
  if (job.status === "pending") {
    status.value = "running";
    phase.value = "submitting";
    message.value = "正在提交下载任务...";
    return;
  }
  status.value = job.status;
  phase.value = job.phase || "";
  message.value = job.message || phaseLabel(job.phase);
}

async function poll(jobId) {
  try {
    const job = await api.getJob(jobId);
    applyJob(job);
    if (job.status === "success") {
      archivedId.value = job.archived_id;
      shareUrl.value = job.share_url || "";
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
  phase.value = "submitting";
  errorMsg.value = "";
  message.value = "正在提交下载任务...";
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
      isbn: props.book.isbn,
      publisher: props.book.publisher,
      account_email: props.accountEmail,
    });
    applyJob(job);
    if (job.status === "success") {
      archivedId.value = job.archived_id;
      shareUrl.value = job.share_url || "";
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

// 阶段文案：轮询间隙（每 2.5s 一次）也始终有文字，避免"卡住"的空白感
function phaseLabel(ph) {
  if (ph === "downloading") return "云端下载中...";
  if (ph === "uploading") return "上传网盘中...";
  if (ph === "submitting") return "正在提交下载任务...";
  return "处理中...";
}

function retry() {
  status.value = "idle";
  phase.value = "";
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
      <span v-else-if="status === 'running'" class="progress-pill">
        <span class="spinner"></span>{{ message || phaseLabel(phase) }}
      </span>
      <div v-else-if="status === 'success'" class="success-actions">
        <button class="btn-dl" @click="downloadLocal(archivedId)">下载到本地</button>
        <button v-if="shareUrl" class="btn-share" @click="copyShare">
          {{ copied ? "已复制" : "复制分享链接" }}
        </button>
        <span v-else class="hint no-share" :title="message">无分享链接</span>
      </div>
      <span v-else-if="status === 'failed'">
        <span class="error">{{ errorMsg }}</span>
        <button @click="retry">重试</button>
      </span>
    </div>
  </div>
</template>

<style scoped>
.progress-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #eef3ff;
  color: #2c5fe0;
  border: 1px solid #c9d8ff;
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 13px;
  white-space: nowrap;
}
.progress-pill .spinner {
  border-color: #b9ccff;
  border-top-color: #2c5fe0;
}
.success-actions {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.btn-dl {
  background: #2c7be5;
  color: #fff;
  border: none;
  padding: 4px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}
.btn-dl:hover {
  background: #1a68d4;
}
.btn-share {
  background: #f5f5f5;
  color: #333;
  border: 1px solid #d0d0d0;
  padding: 3px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}
.btn-share:hover {
  background: #eaeaea;
}
.no-share {
  color: #999;
  font-size: 12px;
}
</style>

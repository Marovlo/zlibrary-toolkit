<script setup>
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import BookRow from "../components/BookRow.vue";

const accounts = ref([]);
const accountEmail = ref("");
const query = ref("");
const forceRefresh = ref(false);

// 两级搜索：先查本地书库（快，不触网），命中就能直接下载；用户明确要联网时
// 才发起真正的云端搜索。stage: idle -> local_checked -> cloud_done
const stage = ref("idle");
const localHits = ref([]);

const loading = ref(false);
const loadingMore = ref(false);
const noMore = ref(false);
const page = ref(1);
const results = ref([]);
const errorMsg = ref("");

// 云端搜索是一次耗时的真实网络请求（服务端可能要解 PoW 挑战 + 排队等代理），
// 用 AbortController 支持"取消"——点取消只是让前端立刻停止等待、恢复可操作，
// 后端那次请求会在后台自然跑完（结果直接丢弃），不影响你接下来做的任何其他
// 操作（下载本地书库的书完全不走网络，跟这个请求毫无关联，不会有冲突）。
let abortController = null;

const DEFAULT_ACCOUNT = "yukirazhang@tencent.com";

async function loadAccounts() {
  try {
    accounts.value = await api.listAccounts();
    // 默认优先选 yukirazhang 这个账号（可用时），否则退回匿名
    const pref = accounts.value.find(
      (a) => a.email === DEFAULT_ACCOUNT && a.available
    );
    if (pref) accountEmail.value = pref.email;
  } catch (e) {
    // 账号列表加载失败不影响匿名搜索，静默忽略
  }
}

// 改了搜索词就重置到初始状态，避免误把旧词的本地/云端结果当成新词的结果。
watch(query, () => {
  if (stage.value !== "idle") {
    stage.value = "idle";
    localHits.value = [];
    results.value = [];
    errorMsg.value = "";
    noMore.value = false;
  }
});

async function checkLocal() {
  const q = query.value.trim();
  if (!q) return;
  errorMsg.value = "";
  stage.value = "local_checked";
  try {
    localHits.value = await api.listArchive(q);
  } catch (e) {
    localHits.value = []; // 本地检查失败不阻塞后续云端搜索，静默忽略
  }
  // 本地书库无匹配时自动发起云端搜索，无需用户再点一次
  if (localHits.value.length === 0) {
    await searchCloud();
  }
}

async function searchCloud() {
  const q = query.value.trim();
  if (!q) return;
  loading.value = true;
  errorMsg.value = "";
  results.value = [];
  page.value = 1;
  noMore.value = false;
  abortController = new AbortController();
  try {
    results.value = await api.search(q, 1, accountEmail.value, forceRefresh.value, abortController.signal);
    stage.value = "cloud_done";
    if (!results.value.length) errorMsg.value = "未找到相关书籍";
  } catch (e) {
    if (e.name !== "AbortError") errorMsg.value = e.message;
  } finally {
    loading.value = false;
    abortController = null;
  }
}

function cancelSearch() {
  abortController?.abort();
}

function onSubmit() {
  if (stage.value === "idle") {
    checkLocal();
  } else {
    searchCloud();
  }
}

// 「加载更多」：翻下一页并追加展示，不对已有结果重新排序——排序仍是站点搜索
// 结果本身的顺序（跟现在的逻辑保持一致），这里只是让用户能看到更多候选。
async function loadMore() {
  if (loadingMore.value || noMore.value) return;
  loadingMore.value = true;
  try {
    const next = page.value + 1;
    const more = await api.search(query.value.trim(), next, accountEmail.value, forceRefresh.value);
    if (!more.length) {
      noMore.value = true;
      return;
    }
    const seen = new Set(results.value.map((b) => `${b.book_id}_${b.hash}`));
    const fresh = more.filter((b) => !seen.has(`${b.book_id}_${b.hash}`));
    if (!fresh.length) {
      noMore.value = true;
      return;
    }
    results.value = results.value.concat(fresh);
    page.value = next;
  } catch (e) {
    errorMsg.value = e.message;
  } finally {
    loadingMore.value = false;
  }
}

function formatSize(bytes) {
  if (!bytes) return "-";
  const mb = bytes / 1048576;
  return mb >= 1 ? `${mb.toFixed(2)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}

onMounted(loadAccounts);
</script>

<template>
  <section class="search">
    <form class="search-bar" @submit.prevent="onSubmit">
      <input v-model="query" placeholder="搜索书名/作者" :disabled="loading" />
      <select v-model="accountEmail" :disabled="loading">
        <option value="">匿名</option>
        <option v-for="a in accounts" :key="a.email" :value="a.email" :disabled="!a.available">
          {{ a.email }}（{{ a.available ? `剩余${a.remaining ?? Math.max(0, a.limit - a.downloads_today)}` : "额度已尽" }}）
        </option>
      </select>
      <button type="submit" :disabled="loading" class="search-btn">
        <span v-if="loading" class="spinner"></span>
        {{ loading ? "云端搜索中，网络较慢时可能需要几十秒..." : stage === "idle" ? "搜索" : "云端搜索" }}
      </button>
      <button v-if="loading" type="button" class="cancel-btn" @click="cancelSearch">取消</button>
    </form>
    <label class="force-refresh">
      <input type="checkbox" v-model="forceRefresh" />
      忽略缓存重新搜索（结果 12 小时内会自动缓存，一般无需勾选）
    </label>

    <div v-if="localHits.length" class="local-hits">
      <p class="hint">本地书库已有 {{ localHits.length }} 本匹配，可直接下载，无需联网：</p>
      <div v-for="b in localHits" :key="b.id" class="book-row">
        <div class="info">
          <div class="title">{{ b.title }}</div>
          <div class="meta">
            {{ b.author || "未知作者" }} · {{ (b.format || "").toUpperCase() }} · {{ formatSize(b.size_bytes) }}
          </div>
        </div>
        <div class="action">
          <a :href="api.archiveFileUrl(b.id)" target="_blank">直接下载</a>
        </div>
      </div>
    </div>
    <p v-else-if="stage === 'local_checked'" class="hint">
      本地书库暂无匹配，点击上方"云端搜索"联网查找
    </p>

    <p v-if="errorMsg" class="error">{{ errorMsg }}</p>
    <div class="results">
      <BookRow
        v-for="(b, idx) in results"
        :key="`${b.book_id}-${b.hash}-${idx}`"
        :book="b"
        :account-email="accountEmail"
      />
    </div>
    <div v-if="results.length" class="load-more">
      <button v-if="!noMore" @click="loadMore" :disabled="loadingMore" class="search-btn">
        <span v-if="loadingMore" class="spinner"></span>
        {{ loadingMore ? "加载中..." : "加载更多结果" }}
      </button>
      <p v-else class="hint">没有更多结果了</p>
    </div>
  </section>
</template>

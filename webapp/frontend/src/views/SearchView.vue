<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";
import BookRow from "../components/BookRow.vue";

const accounts = ref([]);
const accountEmail = ref("");
const query = ref("");
const forceRefresh = ref(false);
const loading = ref(false);
const loadingMore = ref(false);
const noMore = ref(false);
const page = ref(1);
const results = ref([]);
const errorMsg = ref("");

async function loadAccounts() {
  try {
    accounts.value = await api.listAccounts();
  } catch (e) {
    // 账号列表加载失败不影响匿名搜索，静默忽略
  }
}

async function doSearch() {
  if (!query.value.trim()) return;
  loading.value = true;
  errorMsg.value = "";
  results.value = [];
  page.value = 1;
  noMore.value = false;
  try {
    results.value = await api.search(query.value.trim(), 1, accountEmail.value, forceRefresh.value);
    if (!results.value.length) errorMsg.value = "未找到相关书籍";
  } catch (e) {
    errorMsg.value = e.message;
  } finally {
    loading.value = false;
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

onMounted(loadAccounts);
</script>

<template>
  <section class="search">
    <form class="search-bar" @submit.prevent="doSearch">
      <input v-model="query" placeholder="搜索书名/作者" :disabled="loading" />
      <select v-model="accountEmail" :disabled="loading">
        <option value="">匿名</option>
        <option v-for="a in accounts" :key="a.email" :value="a.email" :disabled="!a.available">
          {{ a.email }}（{{ a.available ? `剩余${a.remaining ?? a.limit - a.downloads_today}` : "额度已尽" }}）
        </option>
      </select>
      <button type="submit" :disabled="loading" class="search-btn">
        <span v-if="loading" class="spinner"></span>
        {{ loading ? "搜索中，网络较慢时可能需要几十秒..." : "搜索" }}
      </button>
    </form>
    <label class="force-refresh">
      <input type="checkbox" v-model="forceRefresh" />
      忽略缓存重新搜索（结果 12 小时内会自动缓存，一般无需勾选）
    </label>
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

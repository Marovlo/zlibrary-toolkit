<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const books = ref([]);
const q = ref("");
const copiedId = ref(null);

async function refresh() {
  books.value = await api.listArchive(q.value.trim());
}

async function remove(id) {
  if (!confirm("确认删除这本书的本地存档？")) return;
  await api.deleteArchive(id);
  await refresh();
}

async function copyShare(url, id) {
  try {
    await navigator.clipboard.writeText(url);
    copiedId.value = id;
    setTimeout(() => { if (copiedId.value === id) copiedId.value = null; }, 2000);
  } catch (e) {
    prompt("复制百度云盘链接：", url);
  }
}

function formatSize(bytes) {
  if (!bytes) return "-";
  const mb = bytes / 1048576;
  return mb >= 1 ? `${mb.toFixed(2)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}

onMounted(refresh);
</script>

<template>
  <section class="archive">
    <form class="search-bar" @submit.prevent="refresh">
      <input v-model="q" placeholder="按标题/作者过滤" />
      <button type="submit">筛选</button>
    </form>
    <table>
      <thead>
        <tr>
          <th>标题</th><th>作者</th><th>格式</th><th>大小</th><th>下载时间</th><th>分享链接</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in books" :key="b.id">
          <td>{{ b.title }}</td>
          <td>{{ b.author }}</td>
          <td>{{ (b.format || "").toUpperCase() }}</td>
          <td>{{ formatSize(b.size_bytes) }}</td>
          <td>{{ new Date(b.downloaded_at * 1000).toLocaleString() }}</td>
          <td class="share">
            <button
              v-if="b.share_url"
              class="btn-share"
              @click="copyShare(b.share_url, b.id)"
            >{{ copiedId === b.id ? "已复制" : "复制百度链接" }}</button>
            <span v-else class="no-share">无</span>
          </td>
          <td class="action">
            <a :href="api.archiveFileUrl(b.id)" target="_blank">下载</a>
            &nbsp;
            <button @click="remove(b.id)">删除</button>
          </td>
        </tr>
        <tr v-if="!books.length"><td colspan="7">暂无存档</td></tr>
      </tbody>
    </table>
  </section>
</template>

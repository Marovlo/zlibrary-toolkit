<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api";

const books = ref([]);
const q = ref("");

async function refresh() {
  books.value = await api.listArchive(q.value.trim());
}

async function remove(id) {
  if (!confirm("确认删除这本书的本地存档？")) return;
  await api.deleteArchive(id);
  await refresh();
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
          <th>标题</th><th>作者</th><th>格式</th><th>大小</th><th>下载时间</th><th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in books" :key="b.id">
          <td>{{ b.title }}</td>
          <td>{{ b.author }}</td>
          <td>{{ (b.format || "").toUpperCase() }}</td>
          <td>{{ formatSize(b.size_bytes) }}</td>
          <td>{{ new Date(b.downloaded_at * 1000).toLocaleString() }}</td>
          <td class="action">
            <a :href="api.archiveFileUrl(b.id)" target="_blank">下载</a>
            &nbsp;
            <button @click="remove(b.id)">删除</button>
          </td>
        </tr>
        <tr v-if="!books.length"><td colspan="6">暂无存档</td></tr>
      </tbody>
    </table>
  </section>
</template>

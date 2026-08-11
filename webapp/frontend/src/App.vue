<script setup>
import { ref } from "vue";
import SearchView from "./views/SearchView.vue";
import ArchiveView from "./views/ArchiveView.vue";
import AccountsView from "./views/AccountsView.vue";
import StatusBadge from "./components/StatusBadge.vue";

const tab = ref("search");
</script>

<template>
  <div class="app">
    <header class="topbar">
      <h1>ZLib 私人面板</h1>
      <nav>
        <button :class="{ active: tab === 'search' }" @click="tab = 'search'">搜索</button>
        <button :class="{ active: tab === 'archive' }" @click="tab = 'archive'">书库</button>
        <button :class="{ active: tab === 'accounts' }" @click="tab = 'accounts'">账号</button>
      </nav>
      <StatusBadge />
    </header>
    <main>
      <SearchView v-if="tab === 'search'" />
      <ArchiveView v-else-if="tab === 'archive'" />
      <AccountsView v-else-if="tab === 'accounts'" />
    </main>
  </div>
</template>

<template>
  <el-container style="height:100vh">
    <el-main>
      <el-button @click="runPython">Run Python</el-button>
      <div>{{ output }}</div>
    </el-main>
  </el-container>
</template>

<script setup>
import { ref } from 'vue'
import { invoke } from '@tauri-apps/api'
const output = ref('')
const runPython = async () => {
  output.value = await invoke('run_python')
}
</script>

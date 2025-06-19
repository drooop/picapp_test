import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import App from './App.vue'

createApp(App).use(ElementPlus).use(createPinia()).mount('#app')

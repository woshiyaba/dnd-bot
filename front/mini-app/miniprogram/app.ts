import { sessionStore } from './store/session'

App({
  onLaunch() {
    sessionStore.initialize()
  },
})

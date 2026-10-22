import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ContentAnalysisPanel from '../src/components/ContentAnalysisPanel'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('reconciles an uncertain force POST with a local GET before allowing another fetch', async () => {
  const cached = {novel_id:21,analyzer_version:'content-v1',language:'ja',char_count:1200,
    paragraph_count:8,dialogue_ratio:0.1,image_marker_count:0,structure_score:70,
    is_clutter:false,is_suspect:false,keyword_hits:{},reason_codes:[],analyzed_at:'2026-09-21T00:00:00Z'}
  let cacheReads=0, posts=0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path=new URL(String(input),'http://localhost').pathname
    if(init?.method==='POST') { posts+=1; throw new TypeError('Failed to fetch') }
    if(path==='/api/content/status') return {ok:true,status:200,json:async()=>({available:true})}
    if(path==='/api/content/21') { cacheReads+=1; return {ok:true,status:200,json:async()=>({...cached,char_count:posts?2400:1200})} }
    return {ok:true,status:200,json:async()=>({items:[]})}
  }))
  render(<ContentAnalysisPanel novelId={21} targetState="track" />)
  fireEvent.click(await screen.findByRole('button',{name:'重新抓取并分析'}))
  await waitFor(()=>expect(posts).toBe(1))
  await screen.findByText('分析失败，请稍后重试。')
  const retry=screen.queryByRole('button',{name:/重试本地读取|重试读取|确认.*结果/})
  if(retry) fireEvent.click(retry)
  await waitFor(()=>expect(cacheReads, 'uncertain POST must reconcile through GET, not another force').toBeGreaterThan(1))
  expect(posts).toBe(1)
})

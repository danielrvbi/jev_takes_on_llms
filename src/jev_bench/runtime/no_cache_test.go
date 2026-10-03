package llm

import (
    "encoding/json"
    "fmt"
    "net"
    "net/http"
    "net/http/httptest"
    "testing"
    "github.com/ollama/ollama/api"
    "golang.org/x/sync/semaphore"
)

func TestExperimentNoCacheScoreAndRetry(t *testing.T) {
    calls := 0
    runner := newLlamaScoreTestRunner(t, func(w http.ResponseWriter, r *http.Request) {
        var req scoreCompletionTestRequest
        if err := json.NewDecoder(r.Body).Decode(&req); err != nil { t.Fatal(err) }
        calls++
        if req.CachePrompt || req.NPredict != 1 || len(req.Prompt) != 15 { t.Errorf("cached or primer request: %+v", req) }
        candidate := int(req.LogitBias[0][0])
        if calls == 1 { candidate = 999 } // Exercise the existing internal bias retry.
        _ = json.NewEncoder(w).Encode(scoreCompletionTestResponse(len(req.Prompt), map[int]float64{candidate: 1}))
    })
    runner.options.NumCtx = 64
    result, err := runner.Score(t.Context(), ScoreRequest{MaxTokens: 64, Rows: []ScoreRow{
        {Prompt: "shared-prefix-a", Candidates: []string{"A"}},
        {Prompt: "shared-prefix-b", Candidates: []string{"B"}},
    }})
    if err != nil || calls != 3 || result.InputTokens != 30 { t.Fatalf("result=%+v calls=%d err=%v", result, calls, err) }
}

func TestExperimentNoCacheChatAndCompletion(t *testing.T) {
    calls := 0
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path == "/health" { fmt.Fprint(w, `{"status":"ok"}`); return }
        var body map[string]any
        if err := json.NewDecoder(r.Body).Decode(&body); err != nil { t.Fatal(err) }
        if v, ok := body["cache_prompt"]; !ok || v != false { t.Errorf("cache_prompt must explicitly be false: %+v", body) }
        calls++
        w.Header().Set("Content-Type", "text/event-stream")
        if r.URL.Path == "/v1/chat/completions" {
            fmt.Fprintln(w, `data: {"choices":[{"delta":{},"finish_reason":"stop"}],"timings":{"cache_n":0,"prompt_n":5,"predicted_n":1}}`)
            fmt.Fprintln(w, `data: [DONE]`)
        } else {
            fmt.Fprintln(w, `data: {"content":"A","stop":true,"stop_type":"eos","timings":{"cache_n":0,"prompt_n":5,"predicted_n":1}}`)
        }
    }))
    defer srv.Close()
    runner := &llamaServerRunner{port: srv.Listener.Addr().(*net.TCPAddr).Port, cmd: fakeRunningCmd(), sem: semaphore.NewWeighted(1), options: api.Options{Runner: api.Runner{NumCtx: 2048}}}
    opts := api.DefaultOptions()
    if err := runner.Chat(t.Context(), ChatRequest{Messages: []api.Message{{Role:"user", Content:"test prompt"}}, Options:&opts}, func(ChatResponse){}); err != nil { t.Fatal(err) }
    if err := runner.Completion(t.Context(), CompletionRequest{Prompt:"test prompt", Options:&opts}, func(CompletionResponse){}); err != nil { t.Fatal(err) }
    if calls != 2 { t.Fatalf("calls=%d", calls) }
}

func TestExperimentNoCacheSchemaConversion(t *testing.T) {
    calls := 0
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        var body map[string]any
        if err := json.NewDecoder(r.Body).Decode(&body); err != nil { t.Fatal(err) }
        if v, ok := body["cache_prompt"]; !ok || v != false { t.Errorf("schema conversion must disable prompt cache: %+v", body) }
        calls++
        fmt.Fprint(w, `{"generation_settings/grammar":"root ::= \\\"A\\\""}`)
    }))
    defer srv.Close()
    runner := &llamaServerRunner{port:srv.Listener.Addr().(*net.TCPAddr).Port, client:srv.Client()}
    if _, err := runner.schemaGrammar(t.Context(), json.RawMessage(`{"type":"object","title":"experiment-no-cache"}`)); err != nil { t.Fatal(err) }
    if calls != 1 { t.Fatalf("calls=%d", calls) }
}

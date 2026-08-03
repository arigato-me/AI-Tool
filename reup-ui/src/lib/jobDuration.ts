import { PipelineJob } from "../api";

/** Tổng elapsed_s các bước "resumed" luôn = 0 (xem pipeline_runner.py::_mark_resumed) nên cộng
 * dồn tất cả tự động loại bỏ đúng phần thời gian đã bỏ qua khi resume — không cần lọc riêng. */
export function formatDuration(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds.toFixed(1)}s`;
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}m ${s}s`;
}

export function totalElapsedS(job: PipelineJob): number {
  // GET /pipelines (list) không kèm result.stages đầy đủ — orchestrator tính sẵn total_elapsed_s
  // (xem api.py::list_pipelines). GET /pipelines/{id} (single, JobDetail) không có field này,
  // tự cộng từ result.stages/partial_stages như cũ.
  if (typeof job.total_elapsed_s === "number") return job.total_elapsed_s;
  const stagesData = job.result?.stages ?? job.partial_stages ?? {};
  return Object.values(stagesData).reduce(
    (sum, s) => sum + (typeof s.elapsed_s === "number" ? s.elapsed_s : 0),
    0,
  );
}

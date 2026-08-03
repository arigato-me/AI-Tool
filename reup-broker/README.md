# reup-broker

Redis dùng chung cho toàn bộ node kiến trúc job-queue mới (`reup-*-node`) — vừa là message
queue (reliable, `BLMOVE`) vừa là job store, theo đúng pattern đã review ở `review_tts-node.md`
(gốc repo). Không phải service tự build — dùng thẳng image chính thức `redis:7-alpine`.

Chỉ cần 1 instance cho mọi node (redis rất nhẹ, không cần cô lập theo node) — mỗi node dùng
key-prefix riêng (`queue:pending:<service>`, `job:<service>:<id>`, ...) để không đụng nhau
trong cùng 1 Redis.

## Build & chạy

```bash
cd /u01/reup_tool/docker_build/reup-broker
docker compose up -d
```

Không có `build.context` vì không tự build image — không áp dụng convention `context: ..` của
các service khác trong repo.

## Network

Tạo network Docker tên `reup-net` (khai báo trong chính `docker-compose.yml` này, không phải
`external: true` — đây là service đầu tiên tạo ra network). Mọi `reup-*-node` khác khai báo
network này với `external: true` để join vào, gọi Redis qua tên container `reup-redis` (port
mặc định `6379`, không expose ra host — chỉ nội bộ giữa các container).

**Phải `up` `reup-broker` trước** mọi `reup-*-node` khác (network `reup-net` phải tồn tại sẵn).

## Volume

`./data` (bind-mount, đúng convention hiện tại của `docker_build`) — AOF (`--appendonly yes
--appendfsync everysec`) nên tối đa mất ~1s dữ liệu nếu container/host restart đột ngột, không
mất toàn bộ queue như Redis in-memory thuần.

## Troubleshooting

| Vấn đề | Cách xử lý |
|---|---|
| Node khác báo không kết nối được `reup-redis` | Kiểm tra `reup-broker` đã `up -d` chưa, và network `reup-net` đã tồn tại (`docker network ls`) |
| Mất toàn bộ job sau khi restart Redis | Kiểm tra `./data/appendonly.aof` có tồn tại — nếu volume bị xoá nhầm thì mất thật, AOF chỉ chống crash đột ngột chứ không thay thế backup |

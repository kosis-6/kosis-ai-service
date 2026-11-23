# 운영 서버 빌드 캐시 정리

한국 시간 일요일 오전 4시에 Docker의 오래된 미사용 빌드 캐시를 정리한다. `until=168h`로 최근 일주일의 캐시를 보존한다. 서버가 예약 시점에 꺼져 있으면 다시 켜졌을 때 밀린 작업을 한 번 실행한다.

운영 서버의 저장소 루트에서 타이머를 한 번 등록한다.

```sh
sudo install -m 644 deploy/systemd/kosis-build-cache-prune.service deploy/systemd/kosis-build-cache-prune.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kosis-build-cache-prune.timer
systemctl list-timers kosis-build-cache-prune.timer
```

예약 실행 후 `sudo journalctl -u kosis-build-cache-prune.service`로 종료 상태와 회수한 용량을 확인한다.

# aorus-rtx30-lcd

GIGABYTE **AORUS GeForce RTX 3090 XTREME**의 옆면 LCD(LCD Edge View)를 Windows 없이 Linux에서 제어하는 도구입니다. 제조사는 Windows용 프로그램(GIGABYTE Control Center)만 제공합니다.

> Linux control for the side LCD ("LCD Edge View") of the GIGABYTE AORUS RTX 3090 XTREME, over the GPU's own I2C bus. Images, text and GIFs, on/off, mode switching. No Windows, no vendor software, works inside a GPU-passthrough VM. Unofficial, reverse engineered, use at your own risk.

- 이미지·문구(한글 포함)·GIF 업로드, 켜기/끄기, 모드 전환, 오버레이 설정
- 장치 쪽 도구는 Python 표준 라이브러리만 사용(패키지 설치 불필요)
- GPU를 패스스루 받은 VM 안에서도 동작

## 시험한 환경

| 항목 | 값 |
|---|---|
| 그래픽 카드 | GIGABYTE AORUS GeForce RTX 3090 XTREME 24G (GV-N3090AORUS X-24GD), PCI `10de:2204`, 서브시스템 `1458:4041` |
| LCD 펌웨어 | F2.3 |
| 호스트 | Proxmox VE, GPU를 VM에 PCI 패스스루 |
| GPU를 가진 VM | Ubuntu 24.04.5 LTS, 커널 6.8.0-149, NVIDIA 드라이버 595.91.07(open 커널 모듈), Python 3.12 |
| 용도 | 헤드리스 LLM 추론 서버(llama.cpp) |
| 제어하는 쪽 | 별도의 Linux 장비(Raspberry Pi 5, Python 3 + Pillow 11)에서 SSH로 실행 |

이 한 대에서만 시험했습니다. 같은 펌웨어 계열(F2.3)을 쓰는 RTX 3080/3080 Ti/3090 AORUS MASTER·XTREME도 같은 프로토콜일 가능성이 높지만 확인하지 않았습니다. 50 시리즈는 업로드 주소가 다르므로 이 도구를 쓰지 마세요([다른 프로젝트](#참고한-자료) 참고).

## 구성

| 파일 | 실행 위치 | 역할 |
|---|---|---|
| `aorus_lcd.py` | GPU가 있는 장비(또는 VM), root | I2C로 패널과 통신. 표준 라이브러리만 사용. `/usr/local/sbin/aorus-lcd`로 설치 |
| `lcd` | 아무 Linux 장비 | Pillow로 이미지·문구·GIF를 320×170으로 그려 `aorus-lcd`에 넘김 |
| `tests/` | | 장비 없이 도는 단위 시험 36개 |

## 설치

GPU가 있는 장비에서 `i2c-dev` 모듈이 필요합니다.

```sh
sudo modprobe i2c-dev
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf   # 부팅 시 자동 로드
```

**같은 장비에서 쓸 때** (root로 실행):

```sh
sudo apt install python3-pil        # lcd의 렌더링용
sudo ./lcd install                  # aorus_lcd.py를 /usr/local/sbin/aorus-lcd로 설치하고 점검
```

**다른 장비에서 SSH로 쓸 때** (GPU 장비에는 Pillow가 필요 없습니다):

```sh
export AORUS_LCD_REMOTE="ssh root@gpu-host"
./lcd install
```

설치가 끝나면 자체 점검과 상태가 출력됩니다.

```
ok   F1 header
ok   image container head
ok   image validates
ok   RLE round trip
ok   packet layout
ok   bus /dev/i2c-1 (address 0x61; not opened by selftest)
firmware F2.3
mode     0 (faith1)  raw de 01 01 02
power    on
overlay  temp usage vram  interval 5s  raw df 25 05 02
upload   idle  raw d7 00 05 02
```

## 사용법

```sh
./lcd text "안녕하세요\nLLM VM" --color '#00dcff'    # 문구(여러 줄, 자동 크기)
./lcd image photo.png                                # 이미지 (--fit cover|contain)
./lcd gif anim.gif                                   # GIF (--max-frames, --delay-ms)
./lcd on | off                                       # 켜기 / 끄기
./lcd mode faith1                                    # 내장 화면으로 (0-7 또는 이름)
./lcd overlay temp usage vram --interval 5           # 내장 수치 오버레이 (none으로 끔)
./lcd status                                         # 펌웨어·모드·오버레이 조회
./lcd save                                           # 현재 설정을 전원 재인가 후 기본값으로 저장
```

- `--preview out.png`: 올릴 화면을 PNG로 저장, `--dry-run`: 패널에 보내지 않고 렌더링만
- 모드 이름: `faith1` `faith2` `faith3`(내장 화면) `image` `text` `gif` `chibi` `carousel`
- 이미지 업로드는 약 20초, 작은 GIF는 약 10초 걸립니다.
- 한글 문구에는 Noto CJK 글꼴이 필요합니다(`sudo apt install fonts-noto-cjk`). `--font`로 지정할 수도 있습니다.
- `save`를 하지 않으면 전원이 꺼졌다 켜진 뒤 저장돼 있던 모드로 돌아갑니다. 올린 그림 자체는 패널에 남습니다.

GPU 장비에서 직접 쓸 수 있는 명령은 `aorus-lcd --help`를 보세요. `aorus-lcd feed`는 `nvidia-smi` 값을 패널의 내장 오버레이에 넣어 주는 실험 기능입니다.

## 주의

- **비공식 도구입니다.** 제조사 문서가 아니라 분석으로 알아낸 프로토콜을 씁니다. 사용에 따른 책임은 사용자에게 있습니다.
- **업로드는 패널의 플래시 메모리에 쓰는 동작입니다.** 주기적으로 자동 업로드하지 마세요. 실시간 수치는 내장 오버레이를 쓰세요.
- F2.3 펌웨어는 쓰기 실패를 알려 주지 않습니다. 그림이 깨져 보이면 다시 올리면 됩니다.
- 도구는 LCD 컨트롤러(I2C 주소 `0x61`)에만 씁니다. 같은 버스의 RGB 조명 컨트롤러는 건드리지 않습니다(읽으면 버스가 멈춘다는 보고가 있습니다). RGB 조명 제어는 범위 밖입니다.
- 쓰기 전에 항상 펌웨어 조회(`D6`)에 응답하는지 확인하고, 응답이 없으면 아무것도 쓰지 않습니다.
- 업로드 중 GPU 추론 속도에는 영향이 없었습니다(llama.cpp 59.4 tok/s, 평소 59.4~60.0).

## 프로토콜 요약

- 버스: sysfs 이름이 `NVIDIA i2c adapter 1 at …`인 I2C 어댑터, 7비트 주소 `0x61`. NVIDIA 드라이버의 100kHz 고정 속도에서 동작합니다.
- 명령: 256바이트 쓰기 한 번 `[opcode, CB 55 AC 38, 인자…, 0…]`. 조회는 같은 쓰기 뒤 **정확히 4바이트** 읽기(더 읽으면 I/O 오류).
- 조회: `D6` 펌웨어, `DE` 모드·켜짐, `DF` 오버레이, `D7` 업로드 상태
- 쓰기: `E7 01/02` 켜기/끄기, `E5 <모드+1>` 모드, `E1` 오버레이, `F3` 순환 목록, `AA` 저장
- 업로드: `F2 01` → `F1` 헤더(대상 주소, 저장 유형, 페이지 수, 프레임 수, 지연, 지우기 모드) → 지우기 대기 → 256바이트 페이지 → `F2 02`
- 대상 주소(50 시리즈가 아닌 카드): **이미지 `0x01F25800`**, 텍스트 `0x01F00000`, GIF `0x00000000`
- 화면: 320×170, RGB565 리틀엔디언. 정지 이미지는 무압축, GIF 프레임은 RLE

대상 주소와 대기 시간은 GIGABYTE Control Center 26.09.10.01의 `ucVga.dll`(`GvLcdApi.SendImage`)에서 확인했고, 이 카드(`0x4041`)가 같은 코드의 지원 목록(`IsSupportLcd`)에 있습니다. 제조사 코드나 바이너리는 이 저장소에 없습니다.

## 시험

```sh
python3 -m unittest tests.test_aorus_lcd
```

## 참고한 자료

- [javs/AorusLCD](https://github.com/javs/AorusLCD): RTX 3080 XTREME용 Windows 켜기/끄기 도구. 주소 `0x61`과 명령 형식의 단서
- [albancreton/aorus-master-linux](https://github.com/albancreton/aorus-master-linux), [c-drew/gigabyte-aorus-lcd-linux](https://github.com/c-drew/gigabyte-aorus-lcd-linux), [Maheidem/gigabyte-gpu-lcd](https://github.com/Maheidem/gigabyte-gpu-lcd): RTX 5090용 Linux 구현. RLE 규칙은 c-drew의 설명을 보고 새로 구현했습니다.
- [EKYavsil/aorus-lcd-panel-reverse-engineering](https://github.com/EKYavsil/aorus-lcd-panel-reverse-engineering): LCD 펌웨어 분석(30 시리즈 F2.3 포함)

## 라이선스

MIT

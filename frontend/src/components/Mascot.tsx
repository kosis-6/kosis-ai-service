import large from "../assets/like-snake-mascot-480.webp";
import medium from "../assets/like-snake-mascot-240.webp";
import small from "../assets/like-snake-mascot-96.webp";
import face from "../assets/like-snake-mascot-face-96.webp";

// LIKE SNAKE 캐릭터 (털 질감의 웃는 '6' 뱀). 원본에서 배경 얼룩을 지우고 몸통에 맞춰 잘랐다 (가로:세로 = 885:1140)
const SET = `${small} 75w, ${medium} 186w, ${large} 373w`;

// 보이는 폭: 첫 화면 인사 · 로그인 화면 그림
const SIZES = { welcome: "96px", story: "172px" } as const;

type Props = { place: keyof typeof SIZES; className?: string; label?: string };

/** label이 없으면 꾸밈 그림이라 화면 읽기 프로그램이 건너뛴다 */
export function Mascot({ place, className = "", label }: Props) {
  return (
    <img
      className={`mascot ${className}`.trim()}
      src={medium}
      srcSet={SET}
      sizes={SIZES[place]}
      width={885}
      height={1140}
      alt={label ?? ""}
      decoding="async"
      draggable={false}
    />
  );
}

/** 답 옆 작은 아이콘: 32px에서는 몸 전체가 흐려 얼굴만 잘라 동그라미에 담는다 (꾸밈 그림) */
export function MascotFace() {
  return <img className="mascot-face" src={face} width={96} height={96} alt="" decoding="async" draggable={false} />;
}

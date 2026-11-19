import { Link } from "react-router-dom";

/** 서비스 로고와 이름. 누르면 통계표 찾기 화면으로 간다 */
export function Brand({ className = "" }: { className?: string }) {
  return (
    <Link className={`brand ${className}`.trim()} to="/">
      <span className="logo" role="img" aria-label="LIKE SNAKE 로고" />
      <span className="brand-text">
        <span className="brand-name">LIKE SNAKE</span>
        <span className="brand-tag">통계로 연결되는 일상</span>
      </span>
    </Link>
  );
}

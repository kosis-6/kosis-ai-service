import { Link } from "react-router-dom";
import { Icon } from "../components/Icon";

export function SignupDonePage() {
  return (
    <section className="view done">
      <span className="done-mark">
        <Icon name="check" size={36} stroke={2.25} />
      </span>
      <h1>가입이 완료되었어요</h1>
      <p className="lead">
        LIKE SNAKE에 오신 것을 환영해요.
        <br />
        로그인하고 궁금한 통계를 찾아보세요.
      </p>
      <Link className="btn primary block" to="/login">
        로그인하러 가기
      </Link>
      <Link className="later" to="/">
        먼저 둘러볼게요
      </Link>
    </section>
  );
}

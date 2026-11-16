import { PageHeader } from "../components/ui";
import { CommentsCard } from "../components/CommentsCard";

/** Ответы зрителям — отдельно от выкладки: выбрал каналы, нажал «Пуск». */
export function CommentsPage() {
  return (
    <div className="page narrow">
      <PageHeader
        title="Комментарии"
        subtitle="Сердечко и ответ от имени канала каждому комментарию без ответа. При выкладке ролика это делается само на канале выкладки — здесь можно запустить в любой момент по любым каналам."
      />
      <CommentsCard />
    </div>
  );
}

import { Button, PageHeader } from "../components/ui";
import { SaveTemplateButton } from "../components/SaveTemplate";
import { VoiceTextsCard } from "../components/VoiceTextsCard";
import { useSource } from "../components/SourceCard";
import { VideoOptionsCard, VoiceCard } from "../components/SharedCards";
import { MusicCard } from "../components/MusicCard";
import { MediaModeCard, SlidesIntroCard } from "../components/SectionCards";
import { AiCard } from "../components/AiCard";
import { MixCard } from "../components/MixCard";
import { PublishCard, usePublishCount } from "../components/PublishCard";
import { useLoadedSettings } from "../lib/store";



export function SlidesPage({ goRuns }: { goRuns: () => void }) {
  const { settings } = useLoadedSettings();
  const publishCount = usePublishCount("slides");
  const src = useSource("slides");

  return (
    <div className="page">
      <PageHeader
        title="Стоковые слайды"
        subtitle="Текст → озвучка → фото и видео со стоков по смыслу → ролик 1080p"
        actions={
          <span className="row gap-sm">
            <SaveTemplateButton section="slides" />
            <Button variant="ghost" onClick={goRuns}>
              Запуски →
            </Button>
          </span>
        }
      />
      <div className="page-grid">
        <div className="col-main">
          {src.node}
          <VoiceTextsCard section="slides" />
          <MediaModeCard />
          <SlidesIntroCard />
        </div>
        <div className="col-side">
          <VoiceCard />
          <VideoOptionsCard
            copiesNote={
              settings.slides_publish
                ? `Копий будет ${publishCount} — по числу каналов для выкладки (настройка копий не действует, пока включена выкладка)`
                : undefined
            }
          />
        </div>
      </div>
      <div className="page-cards-2">
        <AiCard />
        <div className="col-stack">
          <MixCard />
          <MusicCard />
        </div>
      </div>
      <PublishCard section="slides" />
      {src.bar}
    </div>
  );
}

import { PageHeader } from "../components/ui";
import { VoiceTextsCard } from "../components/VoiceTextsCard";
import { BrollFootageCard, BrollIntroCard } from "../components/SectionCards";
import { PublishCard, usePublishCount } from "../components/PublishCard";
import { useSource } from "../components/SourceCard";
import { VideoOptionsCard, VoiceCard } from "../components/SharedCards";
import { useLoadedSettings } from "../lib/store";

export function BrollPage() {
  const { settings } = useLoadedSettings();
  const publishCount = usePublishCount();
  const src = useSource("broll");

  return (
    <div className="page">
      <PageHeader
        title="Рассказы"
        subtitle="Текст → озвучка → нарезка ваших футажей с головой диктора и оверлеем"
      />
      <div className="page-grid">
        <div className="col-main">
          {src.node}
          <VoiceTextsCard section="broll" />
          <BrollFootageCard />
          <BrollIntroCard />
        </div>
        <div className="col-side">
          <VoiceCard />
          <VideoOptionsCard
            copiesNote={
              settings.broll_publish
                ? `Копий будет ${publishCount} — по числу каналов для выкладки (настройка копий не действует, пока включена выкладка)`
                : undefined
            }
          />
        </div>
      </div>
      <PublishCard />
      {src.bar}
    </div>
  );
}

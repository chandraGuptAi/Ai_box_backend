"""Shared presentation presets used by both SVG preview and PPTX export."""
THEMES = {
    'corporate': dict(label='Corporate', ink='#152D43', muted='#607184', accent='#4F46E5', light='#EEF1F7', background='#FFFFFF', cover='#152D43', cover_text='#FFFFFF', font='Arial'),
    'minimal': dict(label='Minimal', ink='#202124', muted='#686D76', accent='#2563EB', light='#F1F3F5', background='#FFFFFF', cover='#F1F3F5', cover_text='#202124', font='Arial'),
    'dark': dict(label='Dark', ink='#F1F5F9', muted='#CBD5E1', accent='#38BDF8', light='#263449', background='#111827', cover='#0B1220', cover_text='#F1F5F9', font='Arial'),
    'colorful': dict(label='Colorful', ink='#312E81', muted='#6B5680', accent='#DB2777', light='#FCE7F3', background='#FFF7FB', cover='#581C87', cover_text='#FFFFFF', font='Arial'),
}


def theme_for(report):
    settings = (report.structured_report.scope.get('presentation') or {}) if report.structured_report else {}
    theme = dict(THEMES.get(settings.get('theme_id'), THEMES['corporate']))
    if settings.get('accent'):
        import re
        if re.fullmatch(r'#[0-9A-Fa-f]{6}', settings['accent']):
            theme['accent'] = settings['accent']
    import re
    for key in ('background', 'text_color'):
        value = settings.get(key)
        if isinstance(value, str) and re.fullmatch(r'#[0-9A-Fa-f]{6}', value):
            if key == 'background':
                theme['background'] = theme['cover'] = value
            else:
                theme['ink'] = theme['cover_text'] = theme['muted'] = value
    return theme
